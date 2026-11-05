# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Write and verify the digest manifest of build-artifact-action.

Every packed artefact carries build-artifact.manifest.json beside
build-artifact.tar. The manifest lists every regular file and symlink
in the packed tree with its SHA-256, size and mode bits, or its link
target, and records the tar's own size and SHA-256. The SHA-256 of the
manifest's bytes is the action's 'digest' output, so a consumer that
passes it back as 'expected-digest' pins the content, not the name.

Each failure prints one '::error::' line naming the problem and raises
ManifestError carrying the same message.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import stat
from collections.abc import Iterable, Iterator, Mapping
from typing import cast

from scripts.manifest_schema import (
    ACTION,
    RUN_KEYS,
    SCHEMA,
    FileEntry,
    Manifest,
    ManifestError,
    fail,
    no_duplicates,
    path_problem,
    refuse_constant,
    utf8_ok,
    validate,
)

__all__ = [
    "Manifest",
    "ManifestError",
    "build",
    "load",
    "verify_tar",
    "verify_tree",
    "write",
]

CHUNK_SIZE = 1024 * 1024


def open_regular(
    path: str, *, follow: bool, dir_fd: int | None = None, label: str | None = None
) -> tuple[int, os.stat_result]:
    """Open the regular file at path; return its descriptor and fstat.

    fstat on the open descriptor refuses anything but a regular file,
    and the caller reads metadata and content through that descriptor.
    O_NONBLOCK keeps a FIFO from blocking the open before fstat refuses
    it; it has no effect on reading a regular file. Unless follow is
    set, O_NOFOLLOW refuses a symlink swapped in after a caller's lstat.
    path is relative to dir_fd when given; label names it in errors.
    """
    name = path if label is None else label
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW)
    try:
        fd = os.open(path, flags, dir_fd=dir_fd)
    except OSError as error:
        fail(f"{name}: cannot open: {error.strerror}")
    try:
        info = os.fstat(fd)
    except OSError as error:
        os.close(fd)
        fail(f"{name}: cannot read: {error.strerror}")
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        fail(f"{name}: is not a regular file")
    return fd, info


def read_chunks(fd: int, label: str) -> Iterator[bytes]:
    """Yield the bytes behind fd in chunks, closing fd at the end."""
    try:
        with os.fdopen(fd, "rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                yield chunk
    except OSError as error:
        fail(f"{label}: cannot read: {error.strerror}")


def hash_fd(fd: int, label: str) -> tuple[str, int]:
    """Return the SHA-256 and size of the bytes behind fd, closing it."""
    digest = hashlib.sha256()
    size = 0
    for chunk in read_chunks(fd, label):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def hash_file(path: str) -> tuple[str, int]:
    """Return the SHA-256 and size of the regular file at path."""
    fd, _ = open_regular(path, follow=False)
    return hash_fd(fd, path)


def open_dir(name: str, label: str, dir_fd: int | None) -> int:
    """Open directory name, relative to dir_fd, refusing a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as error:
        fail(f"{label}: cannot open as a directory: {error.strerror}")


def open_parent(root: str, relative: str) -> tuple[int, str]:
    """Return a descriptor for the directory holding root/relative, and its name.

    Each directory from root down is opened relative to the one above
    with O_DIRECTORY | O_NOFOLLOW, so a parent swapped for a symlink
    after the tree was listed is refused, never followed out of root.
    The caller closes the descriptor.
    """
    *parents, name = relative.split("/")
    fd = open_dir(root, f"root {root}", None)
    for depth, segment in enumerate(parents, start=1):
        try:
            child = open_dir(segment, "/".join(parents[:depth]), fd)
        finally:
            os.close(fd)
        fd = child
    return fd, name


def lstat_at(parent: int, name: str, relative: str) -> int:
    """Return the mode of name in directory parent, not following a link."""
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
    except OSError as error:
        fail(f"{relative}: cannot stat: {error.strerror}")


def describe(root: str, relative: str) -> FileEntry | None:
    """Return the manifest entry for root/relative, or None for a directory.

    Never follows a symlink: a symlink is recorded with its target and
    the target is not read. Any other file type is an error.
    """
    if not utf8_ok(relative):
        fail(f"{relative!r}: path is not valid UTF-8")
    parent, name = open_parent(root, relative)
    try:
        return describe_at(parent, name, relative)
    finally:
        os.close(parent)


def describe_at(parent: int, name: str, relative: str) -> FileEntry | None:
    """Return the manifest entry for name in the directory parent."""
    mode = lstat_at(parent, name, relative)
    if stat.S_ISDIR(mode):
        return None
    if stat.S_ISLNK(mode):
        target = read_link(parent, name, relative)
        if not utf8_ok(target):
            fail(f"{relative}: symlink target is not valid UTF-8")
        return {"path": relative, "symlink": target}
    if stat.S_ISREG(mode):
        fd, info = open_regular(name, follow=False, dir_fd=parent, label=relative)
        sha256, size = hash_fd(fd, relative)
        return {
            "path": relative,
            "sha256": sha256,
            "size": size,
            "mode": f"{stat.S_IMODE(info.st_mode):04o}",
        }
    fail(f"{relative}: is not a regular file, symlink or directory")


def read_link(parent: int, name: str, relative: str) -> str:
    """Return the target of symlink name in parent, failing on an OSError."""
    try:
        return os.readlink(name, dir_fd=parent)
    except OSError as error:
        fail(f"{relative}: cannot read symlink: {error.strerror}")


def check_root(root: str) -> str:
    """Return root normalised, failing unless it is a real directory.

    Without this a missing root, a regular file or a symlink would walk
    as an empty tree and pass as an empty artefact. The spelling is
    normalised lexically first, never resolving a symlink, because
    lstat follows a symlink spelled 'linked/' or 'linked/.'; callers
    traverse the spelling checked here.
    """
    normalised = os.path.normpath(root)
    try:
        mode = os.lstat(normalised).st_mode
    except OSError as error:
        fail(f"root {root}: cannot stat: {error.strerror}")
    if not stat.S_ISDIR(mode):
        fail(f"root {root}: is not a directory")
    return normalised


def walk(root: str, relative: str) -> Iterable[str]:
    """Yield relative paths at and below root/relative, not following links."""
    yield relative
    full = os.path.join(root, relative) if relative else root
    if os.path.islink(full) or not os.path.isdir(full):
        return
    try:
        with os.scandir(full) as scan:
            names = sorted(entry.name for entry in scan)
    except OSError as error:
        fail(f"{relative or root}: cannot list directory: {error.strerror}")
    for name in names:
        yield from walk(root, f"{relative}/{name}" if relative else name)


def check_entry(root: str, entry: str) -> None:
    """Fail unless entry is a safe path that exists under root.

    Every directory above the entry must be a real directory, so an
    entry is never read through a symlink.
    """
    problem = path_problem(entry)
    if problem is not None:
        fail(f"entry {entry!r} {problem}")
    segments = entry.split("/")
    for depth in range(1, len(segments)):
        parent = "/".join(segments[:depth])
        full = os.path.join(root, parent)
        if os.path.islink(full) or not os.path.isdir(full):
            fail(f"entry {entry!r} passes through {parent!r}, not a directory")
    if not os.path.lexists(os.path.join(root, entry)):
        fail(f"entry {entry!r} does not exist under {root}")


def tree_entries(root: str, entries: Iterable[str]) -> list[FileEntry]:
    """Return the sorted entries for each path below root in entries.

    An entry of '.' or '' stands for the whole of root. Overlapping
    entries ('dist' and 'dist/a') are listed once.
    """
    root = check_root(root)
    found: dict[str, FileEntry] = {}
    for entry in entries:
        start = "" if entry in ("", ".") else entry
        if start:
            check_entry(root, start)
        for relative in walk(root, start):
            if relative and relative not in found:
                described = describe(root, relative)
                if described is not None:
                    found[relative] = described
    return [found[path] for path in sorted(found)]


def build(
    root: str,
    entries: Iterable[str],
    *,
    artifact_name: str,
    tar_path: str,
    root_name: str | None = None,
    env: Mapping[str, str] | None = None,
    created_at: str | None = None,
) -> Manifest:
    """Return the manifest for entries under root and the tar at tar_path.

    root_name defaults to the last component of root, taken from the
    normalised spelling that is traversed; the run keys come from env,
    by default the process environment; created_at defaults to the
    current UTC time to the second.
    """
    environ = os.environ if env is None else env
    root = check_root(root)
    if root_name is None:
        root_name = os.path.basename(os.path.realpath(root))
    problem = path_problem(root_name)
    if problem is not None:
        fail(f"root name {root_name!r} {problem}")
    files = tree_entries(root, entries)
    tar_sha256, tar_size = hash_file(tar_path)
    if created_at is None:
        now = datetime.datetime.now(datetime.UTC)
        created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": SCHEMA,
        "action": ACTION,
        "artifact_name": artifact_name,
        "root": root_name,
        "created_at": created_at,
        "run_id": environ.get(RUN_KEYS["run_id"], ""),
        "run_attempt": environ.get(RUN_KEYS["run_attempt"], ""),
        "repository": environ.get(RUN_KEYS["repository"], ""),
        "sha": environ.get(RUN_KEYS["sha"], ""),
        "tar": {
            "path": os.path.basename(tar_path),
            "sha256": tar_sha256,
            "size": tar_size,
        },
        "files": files,
    }


def canonical(manifest: Manifest) -> bytes:
    """Return the canonical bytes of manifest: sorted keys, 2-space indent."""
    text = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def write(manifest: Manifest, path: str) -> str:
    """Write manifest to path in canonical form; return its SHA-256."""
    data = canonical(manifest)
    try:
        with open(path, "wb") as handle:
            _ = handle.write(data)
    except OSError as error:
        fail(f"{path}: cannot write: {error.strerror}")
    return hashlib.sha256(data).hexdigest()


def load(path: str) -> Manifest:
    """Parse and validate the manifest at path."""
    fd, _ = open_regular(path, follow=True)
    data = b"".join(read_chunks(fd, path))
    try:
        parsed: object = json.loads(  # pyright: ignore[reportAny]
            data.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=refuse_constant,
        )
    except (UnicodeDecodeError, ValueError) as error:
        fail(f"{path}: not valid UTF-8 JSON: {error}")
    return validate(parsed)


def verify_tar(manifest: Manifest, tar_path: str) -> None:
    """Fail unless the tar at tar_path has the size and SHA-256 recorded."""
    sha256, size = hash_file(tar_path)
    expected = manifest["tar"]
    if size != expected["size"]:
        fail(f"{tar_path}: size {size} does not match manifest {expected['size']}")
    if sha256 != expected["sha256"]:
        fail(f"{tar_path}: SHA-256 does not match the manifest")


def verify_entry(root: str, entry: FileEntry) -> None:
    """Fail unless root/path matches entry exactly.

    The entry is reached through directory descriptors (open_parent), and
    a regular file's mode, size and SHA-256 all come from one descriptor.
    """
    parent, name = open_parent(root, entry["path"])
    try:
        verify_at(parent, name, entry)
    finally:
        os.close(parent)


def verify_at(parent: int, name: str, entry: FileEntry) -> None:
    """Fail unless name in the directory parent matches entry exactly."""
    path = entry["path"]
    mode = lstat_at(parent, name, path)
    if "symlink" in entry:
        # mypy does not narrow a TypedDict union on 'in'; pyright does.
        target = cast("Mapping[str, object]", entry)["symlink"]
        if not stat.S_ISLNK(mode):
            fail(f"{path}: must be a symlink")
        if read_link(parent, name, path) != target:
            fail(f"{path}: symlink target does not match the manifest")
        return
    if not stat.S_ISREG(mode):
        fail(f"{path}: must be a regular file")
    fd, info = open_regular(name, follow=False, dir_fd=parent, label=path)
    actual = f"{stat.S_IMODE(info.st_mode):04o}"
    if actual != entry["mode"]:
        os.close(fd)
        fail(f"{path}: mode {actual} is not {entry['mode']}")
    sha256, size = hash_fd(fd, path)
    if size != entry["size"]:
        fail(f"{path}: size {size} does not match manifest {entry['size']}")
    if sha256 != entry["sha256"]:
        fail(f"{path}: SHA-256 does not match the manifest")


def verify_tree(manifest: Manifest, root: str) -> int:
    """Fail unless the tree under root is exactly what manifest lists.

    Every listed entry must exist with its recorded type, size, SHA-256,
    mode bits or symlink target, and nothing but directories may exist
    under root unlisted. root itself must be a real directory. Symlinks
    are never followed. Returns the number of entries checked.
    """
    root = check_root(root)
    listed = {entry["path"]: entry for entry in manifest["files"]}
    present: set[str] = set()
    for relative in walk(root, ""):
        full = os.path.join(root, relative) if relative else root
        if relative and (os.path.islink(full) or not os.path.isdir(full)):
            present.add(relative)
    missing = sorted(listed.keys() - present)
    if missing:
        fail(f"{missing[0]}: listed in the manifest but missing")
    extra = sorted(present - listed.keys())
    if extra:
        fail(f"{extra[0]}: present but not listed in the manifest")
    for path in sorted(listed):
        verify_entry(root, listed[path])
    return len(listed)
