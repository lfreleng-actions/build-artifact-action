# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The 'pack' mode of build-artifact-action.

Reads the normalised inputs the validation step wrote, packs the
selected entries under the packed root into a reproducible tar, writes
the digest manifest beside it in a fresh directory under RUNNER_TEMP,
and reports the two paths for the upload step. The same tree at the
same commit always gives a byte-identical tar: members are sorted by
path, owners cleared, modes masked to 0777 and times fixed to the
commit time of GITHUB_SHA, or to the epoch where git cannot give one.
The manifest, and so the digest, is byte-identical only where the
artefact name, root name, repository, GITHUB_SHA, run ID and run
attempt match too, since it records them.
"""

from __future__ import annotations

import os
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Mapping
from typing import NoReturn, cast

# 'python3 -I' leaves the action's own directory off sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.manifest import build, write
from scripts.manifest_schema import FileEntry, ManifestError, fail, utf8_ok
from scripts.tarball import (
    MODE_MASK,
    Member,
    check_member,
    check_members,
    check_root_dir,
    write_tar,
)
from scripts.unpack import MAX_DEPTH
from scripts.unpack_links import check_link_target
from scripts.validate import inside, write_outputs

TAR_NAME = "build-artifact.tar"
MANIFEST_NAME = "build-artifact.manifest.json"
SCRATCH_PREFIX = "build-artifact-"
GIT_DIR = ".git"
COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
EXTRAHEADER_RE = re.compile(r"\s*extraheader\s*(=|$)", re.IGNORECASE)
GIT_TIMEOUT_SECONDS = 30


def walk_error(error: OSError) -> NoReturn:
    """Fail on a directory os.walk cannot list."""
    fail(f"cannot list directory: {error}")


def check_root(env: Mapping[str, str]) -> str:
    """Return the packed root, failing unless it is a real directory.

    The root must already be canonical, which leaves no symlink in any
    component, and must lie inside the canonical workspace, outside
    any '.git' directory.
    """
    workspace_env = env.get("GITHUB_WORKSPACE", "")
    if not workspace_env:
        fail("GITHUB_WORKSPACE: must be set")
    workspace = os.path.realpath(workspace_env)
    root = env.get("PACK_ROOT", "")
    if not os.path.isabs(root) or os.path.realpath(root) != root:
        fail(f"PACK_ROOT: {root!r} must be an absolute path with no symlink")
    if not inside(root, workspace):
        fail(f"PACK_ROOT: {root} must lie inside GITHUB_WORKSPACE")
    if GIT_DIR in os.path.relpath(root, workspace).split(os.sep):
        fail(f"PACK_ROOT: {root} lies in {GIT_DIR}/, which is never packed")
    if not os.path.isdir(root):
        fail(f"PACK_ROOT: {root} is not a directory")
    return root


def check_credentials(root: str) -> None:
    """Fail when root/.git/config holds an http extraheader credential."""
    config = os.path.join(root, GIT_DIR, "config")
    if not os.path.isfile(config):
        return
    try:
        with open(config, encoding="utf-8", errors="replace") as handle:
            found = any(EXTRAHEADER_RE.match(line) for line in handle)
    except OSError as error:
        fail(f"{config}: cannot read: {error.strerror}")
    if found:
        hint = "check out with 'persist-credentials: false' before packing"
        fail(f"{config}: holds an http extraheader credential; {hint}")


def skipped(root: str, full: str, temp: str) -> bool:
    """Return whether full lies in RUNNER_TEMP below the root, never packed.

    RUNNER_TEMP holds the runner's output and step summary files and the
    SCRATCH_PREFIX directories this action writes its tar and manifest
    to, from this run and earlier ones, and packing them would change
    the tar from run to run. run() refuses a root that is RUNNER_TEMP.
    """
    return temp != root and inside(temp, root) and inside(full, temp)


def check_entry(root: str, entry: str, temp: str) -> None:
    """Fail unless the paths entry exists inside root, reached directly."""
    segments = entry.split("/")
    if GIT_DIR in segments:
        fail(f"paths: {entry!r} lies in {GIT_DIR}/, which is never packed")
    full = os.path.join(root, entry)
    if not os.path.lexists(full):
        fail(f"paths: {entry!r} does not exist under {root}")
    if not inside(os.path.realpath(full), root):
        fail(f"paths: {entry!r} resolves outside {root}")
    if skipped(root, os.path.normpath(full), temp):
        fail(f"paths: {entry!r} lies in RUNNER_TEMP, which is never packed")
    for depth in range(1, len(segments)):
        parent = "/".join(segments[:depth])
        if os.path.islink(os.path.join(root, parent)):
            fail(f"paths: {entry!r} passes through the symlink {parent!r}")


def collect(root: str, start: str, temp: str, found: dict[str, os.stat_result]) -> None:
    """Add start and everything below it to found, never following links.

    '.git' is skipped at any depth, and so is the runner scratch under
    temp that skipped() names.
    """
    top = os.path.join(root, start) if start else root
    try:
        info = os.lstat(top)
    except OSError as error:
        fail(f"{start}: cannot stat: {error.strerror}")
    if start:
        found[start] = info
    if not stat.S_ISDIR(info.st_mode):
        return
    for dirpath, dirnames, filenames in os.walk(top, onerror=walk_error):
        dirnames[:] = [
            name
            for name in dirnames
            if name != GIT_DIR and not skipped(root, os.path.join(dirpath, name), temp)
        ]
        for name in dirnames + filenames:
            if name == GIT_DIR:
                continue
            full = os.path.join(dirpath, name)
            try:
                found[os.path.relpath(full, root)] = os.lstat(full)
            except OSError as error:
                fail(f"{os.path.relpath(full, root)}: cannot stat: {error.strerror}")


def select(root: str, paths: str, temp: str) -> tuple[dict[str, os.stat_result], bool]:
    """Return every entry selected by paths under root, keyed by path.

    An entry is checked as spelled, then collected under its normalised
    spelling, the one the manifest records; one normalising to '.'
    selects the whole root, which the returned flag reports.
    """
    entries = [line.strip() for line in paths.splitlines() if line.strip()]
    found: dict[str, os.stat_result] = {}
    whole = False
    for entry in entries or ["."]:
        if entry != ".":
            check_entry(root, entry, temp)
        normalised = posixpath.normpath(entry)
        whole = whole or normalised == "."
        collect(root, "" if normalised == "." else normalised, temp, found)
    return found, whole


def check_symlink(root: str, path: str) -> str:
    """Return the target of the symlink root/path, failing if it escapes.

    The target must pass the rule unpack applies to every symlink it
    extracts, which has no '..' after a named segment, so no chain of
    links can leave the root and re-enter it. It must also resolve
    inside root here, through the symlinks it passes.
    """
    try:
        target = os.readlink(os.path.join(root, path))
    except OSError as error:
        fail(f"{path}: cannot read symlink: {error.strerror}")
    if not utf8_ok(target):
        fail(f"{path}: symlink target is not valid UTF-8")
    check_link_target(path, target)
    if not inside(
        os.path.realpath(os.path.join(root, os.path.dirname(path), target)), root
    ):
        fail(f"{path}: symlink target {target!r} leaves the packed root")
    return target


def file_kind(mode: int) -> str:
    """Name a file type that cannot be packed."""
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return "a device"
    return "not a regular file, symlink or directory"


def members_of(root: str, found: Mapping[str, os.stat_result]) -> list[Member]:
    """Return the tar members for found, sorted by path, or fail.

    A path nested as deep as unpack's MAX_DEPTH is refused, as unpack
    would refuse it. A regular file with more links than the selection
    holds is hard-linked to a file outside it, and is refused. A
    symlink must still be the one selected once its target is read, or
    the target could be a replacement's.
    """
    members: list[Member] = []
    for path in sorted(found):
        info = found[path]
        if not utf8_ok(path):
            fail(f"{path!r}: path is not valid UTF-8")
        if path.count("/") >= MAX_DEPTH:
            fail(f"{path}: nested deeper than {MAX_DEPTH} levels, which unpack refuses")
        if stat.S_ISLNK(info.st_mode):
            member = Member(path, info, target=check_symlink(root, path))
            check_member(root, member)
            members.append(member)
        elif stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            members.append(Member(path, info))
        else:
            fail(f"{path}: is {file_kind(info.st_mode)}, which cannot be packed")
    linked = [
        m for m in members if stat.S_ISREG(m.info.st_mode) and m.info.st_nlink > 1
    ]
    links = Counter((m.info.st_dev, m.info.st_ino) for m in linked)
    for member in linked:
        info = member.info
        if info.st_nlink > links[(info.st_dev, info.st_ino)]:
            fail(f"{member.path}: is hard-linked to a file outside the selection")
    return members


def commit_time(root: str, env: Mapping[str, str]) -> int:
    """Return the commit time of GITHUB_SHA, or 0 where git gives none."""
    sha = env.get("GITHUB_SHA", "")
    if COMMIT_RE.fullmatch(sha) is None:
        return 0
    try:
        result = subprocess.run(
            ["git", "-C", root, "show", "-s", "--format=%ct", sha],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    value = result.stdout.strip()
    if result.returncode != 0 or not value.isdigit():
        return 0
    return int(value)


def as_packed(entry: FileEntry, packed: Mapping[str, Member]) -> FileEntry:
    """Return entry as the tar holds it, failing if the entry changed.

    The entry must have the type, content, masked mode or symlink target
    the tar recorded, so an unpacked copy verifies against the manifest.
    The manifest records mode bits as the tar does, masked to 0777.
    """
    path = entry["path"]
    member = packed.get(path)
    if member is None:
        fail(f"{path}: appeared while it was being packed")
    fields = cast("Mapping[str, object]", entry)
    if "symlink" in fields:
        if not stat.S_ISLNK(member.info.st_mode) or fields["symlink"] != member.target:
            fail(f"{path}: changed while it was being packed")
        return entry
    mode = int(cast("str", fields["mode"]), 8) & MODE_MASK
    archived = (member.sha256, member.size, member.mode)
    if (
        not stat.S_ISREG(member.info.st_mode)
        or (fields["sha256"], fields["size"], mode) != archived
    ):
        fail(f"{path}: changed while it was being packed")
    return {
        "path": path,
        "sha256": member.sha256,
        "size": member.size,
        "mode": f"{mode:04o}",
    }


def summarise(path: str, name: str, count: int, size: int, digest: str) -> None:
    """Append the artefact's name, file count, bytes and digest to path."""
    lines = [
        "### 📦 Packed artefact",
        "",
        "| Name | Files | Bytes | Digest |",
        "| ---- | ----- | ----- | ------ |",
        f"| `{name}` | {count} | {size} | `{digest}` |",
        "",
    ]
    try:
        with open(path, "a", encoding="utf-8") as handle:
            _ = handle.write("\n".join(lines))
    except OSError as error:
        fail(f"GITHUB_STEP_SUMMARY: cannot write: {error.strerror}")


def run(env: Mapping[str, str]) -> int:
    """Pack the selected tree and report the files to upload."""
    root = check_root(env)
    name = env.get("PACK_ARTIFACT_NAME", "")
    fail_on_empty = env.get("PACK_FAIL_ON_EMPTY", "")
    if fail_on_empty not in ("true", "false"):
        fail("fail-on-empty: must be exactly 'true' or 'false'")
    output = env.get("GITHUB_OUTPUT", "")
    if not output:
        fail("GITHUB_OUTPUT: must be set")
    runner_temp = env.get("RUNNER_TEMP", "")
    if not runner_temp:
        fail("RUNNER_TEMP: must be set")
    check_credentials(root)

    temp = os.path.realpath(runner_temp)
    if temp == root:
        fail(f"PACK_ROOT: {root} is RUNNER_TEMP, which holds the runner's own files")
    scratch = os.path.realpath(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=temp))
    try:
        root_info = os.lstat(root)
    except OSError as error:
        fail(f"{root}: cannot stat: {error.strerror}")
    found, whole = select(root, env.get("PACK_PATHS", ""), temp)
    members = members_of(root, found)
    files = [m.path for m in members if not stat.S_ISDIR(m.info.st_mode)]
    if not files and fail_on_empty == "true":
        fail(f"fail-on-empty: nothing to pack under {root}")

    mtime = commit_time(root, env)
    tar_path = os.path.join(scratch, TAR_NAME)
    # An empty selection still checks its directories below, but the
    # tar must stay empty.
    write_tar(root, members if files else [], tar_path, mtime)
    manifest = build(
        root,
        files,
        artifact_name=name,
        tar_path=tar_path,
        env=env,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)),
    )
    packed = {m.path: m for m in members if not stat.S_ISDIR(m.info.st_mode)}
    manifest["files"] = [as_packed(entry, packed) for entry in manifest["files"]]
    if len(manifest["files"]) != len(packed):
        fail(f"{root}: an entry became a directory while it was being packed")
    check_members(root, members)
    if whole:
        check_root_dir(root, root_info)
    manifest_path = os.path.join(scratch, MANIFEST_NAME)
    digest = write(manifest, manifest_path)

    size = sum(m.size for m in members)
    write_outputs(
        output,
        {
            "tar": tar_path,
            "manifest": manifest_path,
            "digest": digest,
            "file-count": str(len(files)),
        },
    )
    summary = env.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        summarise(summary, name, len(files), size, digest)
    print(f"Packed {len(files)} files ({size} bytes) as '{name}' ✅")
    return 0


def main() -> int:
    """Run pack mode against the process environment."""
    try:
        return run(os.environ)
    except ManifestError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
