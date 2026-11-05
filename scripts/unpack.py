# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The 'unpack' mode of build-artifact-action.

The artefact is untrusted input: the producer job ran the project's
code, and the consumer may hold a credential. This step takes the
directory actions/download-artifact filled, checks it against its
manifest (and 'expected-digest' when given), checks every tar member
before extracting any, extracts into a newly created directory under
the workspace with tarfile's 'data' filter, and verifies the tree
against the manifest. Nothing it extracts is executed.

Inputs come from the environment: INPUT_ARTIFACT_NAME,
INPUT_DESTINATION, INPUT_EXPECTED_DIGEST, DOWNLOAD_DIR and the runner's
GITHUB_* variables. Each refusal prints one '::error::' line naming the
rule and exits 1, removing anything this step created.
"""

from __future__ import annotations

import copy
import os
import posixpath
import shutil
import stat
import sys
import tarfile
import warnings
from collections.abc import Mapping
from typing import cast

# python3 -I leaves the action's own root off sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from scripts.manifest import (
    Manifest,
    ManifestError,
    hash_file,
    load,
    verify_tar,
    verify_tree,
)
from scripts.manifest_schema import (
    RegularEntry,
    escape_annotation,
    fail,
    path_problem,
)
from scripts.unpack_links import check_link_target, restore_link_targets
from scripts.unpack_modes import as_restored, drop_owner_read, restore_modes
from scripts.validate import PATH_RULE, inside, path_grammar_ok, write_outputs

TAR_NAME = "build-artifact.tar"
MANIFEST_NAME = "build-artifact.manifest.json"
# Verifying and removing a tree recurse once per directory level, so a
# bound far inside Python's recursion limit keeps both from failing.
MAX_DEPTH = 256


def require(env: Mapping[str, str], key: str) -> str:
    """Return env[key], failing when it is unset or empty."""
    value = env.get(key, "")
    if not value:
        fail(f"{key}: must be set")
    return value


def plan_destination(workspace: str, value: str) -> tuple[str, list[str]]:
    """Return the absolute destination and its missing ancestors, in order.

    workspace is already canonical. Every existing component below it
    must be a real directory, never a symlink, and the destination
    itself must not exist: only a newly created root guarantees that no
    pre-existing link beneath it can redirect a write.
    """
    if not path_grammar_ok(value):
        fail(f"destination: {PATH_RULE}")
    # The grammar admits '.' segments, which would otherwise survive into
    # the paths created and checked below.
    value = posixpath.normpath(value)
    if value == ".":
        fail("destination: must not exist; '.' is the workspace itself")
    segments = value.split("/")
    current = workspace
    missing: list[str] = []
    for depth, segment in enumerate(segments[:-1], start=1):
        current = os.path.join(current, segment)
        if missing:
            missing.append(current)
            continue
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            missing.append(current)
            continue
        except OSError as error:
            fail(f"destination: cannot stat {current}: {error.strerror}")
        ancestor = "/".join(segments[:depth])
        if stat.S_ISLNK(mode):
            fail(f"destination: ancestor {ancestor!r} is a symlink")
        if not stat.S_ISDIR(mode):
            fail(f"destination: ancestor {ancestor!r} is not a directory")
    final = os.path.join(current, segments[-1])
    if not missing and os.path.lexists(final):
        fail(f"destination: {value!r} must not exist; unpack creates it")
    return final, missing


def create_destination(
    workspace: str, final: str, missing: list[str], created: list[str]
) -> None:
    """Create each missing ancestor, then final, recording each in created."""
    for path in [*missing, final]:
        try:
            os.mkdir(path)
        except FileExistsError:
            fail(f"destination: {path} must not exist; unpack creates it")
        except OSError as error:
            fail(f"destination: cannot create {path}: {error.strerror}")
        created.append(path)
    if os.path.realpath(final) != final or not inside(final, workspace):
        fail(f"destination: {final} does not resolve inside the workspace")


def remove_created(created: list[str]) -> list[str]:
    """Remove what create_destination made: the tree, then empty parents.

    Returns an error message for each path left behind.
    """
    problems: list[str] = []
    for path in reversed(created):
        try:
            if path == created[-1]:
                shutil.rmtree(path)
            else:
                os.rmdir(path)
        except OSError as error:
            problems.append(f"cannot remove {path}: {error.strerror}")
    return problems


def check_download(directory: str) -> tuple[str, str]:
    """Return the tar and manifest paths in directory, which holds only those."""
    try:
        names = sorted(os.listdir(directory))
    except OSError as error:
        fail(f"DOWNLOAD_DIR: cannot list {directory}: {error.strerror}")
    expected = sorted((MANIFEST_NAME, TAR_NAME))
    if names != expected:
        found = ", ".join(repr(name) for name in names) or "nothing"
        fail(f"download must hold exactly {' and '.join(expected)}; found {found}")
    for name in names:
        try:
            mode = os.lstat(os.path.join(directory, name)).st_mode
        except OSError as error:
            fail(f"download: cannot stat {name}: {error.strerror}")
        if not stat.S_ISREG(mode):
            fail(f"download: {name} is not a regular file")
    return os.path.join(directory, TAR_NAME), os.path.join(directory, MANIFEST_NAME)


def verify_download(
    env: Mapping[str, str], tar_path: str, manifest_path: str
) -> tuple[Manifest, str]:
    """Return the manifest and its digest once it vouches for the tar."""
    manifest = load(manifest_path)
    name = require(env, "INPUT_ARTIFACT_NAME")
    if manifest["artifact_name"] != name:
        recorded = manifest["artifact_name"]
        fail(f"artifact_name: manifest names {recorded!r}, not {name!r}")
    run_id = require(env, "GITHUB_RUN_ID")
    if manifest["run_id"] != run_id:
        fail(f"run_id: artefact comes from another run ({manifest['run_id']!r})")
    digest, _ = hash_file(manifest_path)
    expected = env.get("INPUT_EXPECTED_DIGEST", "")
    if expected and digest != expected:
        fail(f"expected-digest: manifest SHA-256 {digest} is not {expected}")
    if manifest["tar"]["path"] != TAR_NAME:
        fail(f"tar.path: must be {TAR_NAME}")
    verify_tar(manifest, tar_path)
    return manifest, digest


def check_parents(name: str, kinds: Mapping[str, str]) -> None:
    """Fail when a parent of name is an earlier symlink or file member."""
    segments = name.split("/")
    for depth in range(1, len(segments)):
        parent = "/".join(segments[:depth])
        kind = kinds.get(parent)
        if kind == "symlink":
            fail(f"tar member {name!r}: would be written through symlink {parent!r}")
        if kind not in (None, "dir"):
            fail(f"tar member {name!r}: lies below file member {parent!r}")


def member_kind(
    member: tarfile.TarInfo,
    kinds: Mapping[str, str],
    files: Mapping[str, tarfile.TarInfo],
) -> str:
    """Return the kind of member, failing on any type unpack refuses."""
    name = member.name
    if member.isdir():
        return "dir"
    if member.isreg():
        return "file"
    if member.islnk():
        target = member.linkname
        if kinds.get(target) != "file":
            fail(f"tar member {name!r}: hard link {target!r} is not an earlier file")
        # Both names share one inode, so they cannot keep different modes.
        if member.mode & 0o7777 != files[target].mode & 0o7777:
            fail(f"tar member {name!r}: hard link mode differs from {target!r}")
        return "hardlink"
    if member.issym():
        check_link_target(name, member.linkname)
        return "symlink"
    if member.isdev():
        fail(f"tar member {name!r}: device or FIFO member")
    fail(f"tar member {name!r}: unsupported member type")


def scan_members(members: list[tarfile.TarInfo]) -> dict[str, tarfile.TarInfo]:
    """Fail on any unsafe member; return the non-directory members by name."""
    kinds: dict[str, str] = {}
    implied: set[str] = set()
    files: dict[str, tarfile.TarInfo] = {}
    for member in members:
        name = member.name
        problem = path_problem(name)
        if problem is not None:
            fail(f"tar member {name!r}: {problem}")
        if name.count("/") >= MAX_DEPTH:
            fail(f"tar member {name!r}: nested deeper than {MAX_DEPTH} levels")
        if name in kinds:
            fail(f"tar member {name!r}: appears more than once")
        check_parents(name, kinds)
        kind = member_kind(member, kinds, files)
        if kind != "dir":
            if name in implied:
                fail(f"tar member {name!r}: replaces a directory")
            files[name] = member
        kinds[name] = kind
        segments = name.split("/")
        implied.update("/".join(segments[:depth]) for depth in range(1, len(segments)))
    return files


def match_manifest(manifest: Manifest, files: Mapping[str, tarfile.TarInfo]) -> None:
    """Fail unless the tar's files and the manifest's entries are the same set."""
    listed = {entry["path"]: entry for entry in manifest["files"]}
    for name, member in files.items():
        entry = listed.get(name)
        if entry is None:
            fail(f"tar member {name!r}: not listed in the manifest")
        # mypy does not narrow a TypedDict union on 'in'; pyright does.
        target = cast("Mapping[str, object]", entry).get("symlink")
        if member.issym() != (target is not None) or (
            member.issym() and member.linkname != target
        ):
            fail(f"tar member {name!r}: type or target does not match the manifest")
        if member.issym():
            continue
        regular = cast("RegularEntry", entry)
        if member.isreg() and member.size != regular["size"]:
            fail(f"tar member {name!r}: size does not match the manifest")
        if f"{member.mode & 0o7777:04o}" != regular["mode"]:
            fail(f"tar member {name!r}: mode does not match the manifest")
    missing = sorted(listed.keys() - files.keys())
    if missing:
        fail(f"{missing[0]}: listed in the manifest but missing from the tar")


def has_data_filter() -> bool:
    """Return whether this interpreter's tarfile has extraction filters."""
    return hasattr(tarfile, "data_filter")


def data_rules(member: tarfile.TarInfo, root: str) -> tarfile.TarInfo:
    """Return member as the 'data' filter would extract it, or fail.

    Used only where tarfile lacks the filter: the member and any link
    target must resolve inside root, ownership is dropped, and modes
    lose their high bits.
    """
    name = member.name
    target = os.path.realpath(os.path.join(root, name))
    if target == root or not inside(target, root):
        fail(f"tar member {name!r}: resolves outside the destination")
    if member.issym() or member.islnk():
        base = os.path.dirname(name) if member.issym() else ""
        link = os.path.realpath(os.path.join(root, base, member.linkname))
        if not inside(link, root):
            fail(f"tar member {name!r}: link resolves outside the destination")
    clean = copy.copy(member)
    clean.uid, clean.gid = os.geteuid(), os.getegid()
    clean.uname = clean.gname = ""
    if member.isdir():
        clean.mode = 0o755
    elif not member.issym():
        clean.mode = (member.mode & 0o755) | 0o600
    return clean


def extract(tar_path: str, root: str, members: list[tarfile.TarInfo]) -> None:
    """Extract the scanned tar into root, never running tar(1).

    PAX metadata the scan does not judge, such as an out-of-range mtime,
    can make tarfile raise ValueError or OverflowError part-way through.
    A long chain of symlinks, each pointing at the one before, can make
    the realpath behind the containment checks raise RecursionError.
    """
    try:
        with tarfile.open(tar_path, "r:") as archive:
            if has_data_filter():
                archive.extractall(root, numeric_owner=False, filter="data")
                return
            with warnings.catch_warnings():
                # data_rules stands in for the filter tarfile lacks here.
                warnings.simplefilter("ignore", DeprecationWarning)
                for member in members:
                    archive.extract(data_rules(member, root), root, numeric_owner=False)
    except RecursionError:
        fail(f"{TAR_NAME}: extraction failed: symlink chain too long to resolve")
    except (tarfile.TarError, OSError, ValueError, OverflowError) as error:
        fail(f"{TAR_NAME}: extraction failed: {error}")


def read_members(tar_path: str) -> list[tarfile.TarInfo]:
    """Return every member of the tar at tar_path without extracting any.

    Malformed PAX metadata, such as a non-numeric GNU.sparse.map, makes
    tarfile raise ValueError or OverflowError while it reads headers.
    tarfile reads each extended header by recursing into the next, so a
    long run of them raises RecursionError.
    """
    try:
        with tarfile.open(tar_path, "r:") as archive:
            return archive.getmembers()
    except RecursionError:
        fail(f"{TAR_NAME}: cannot read as an uncompressed tar: header chain too long")
    except (tarfile.TarError, OSError, ValueError, OverflowError) as error:
        fail(f"{TAR_NAME}: cannot read as an uncompressed tar: {error}")


def unpack(env: Mapping[str, str], created: list[str]) -> dict[str, str]:
    """Verify, extract and check the artefact; return the step outputs."""
    workspace = os.path.realpath(require(env, "GITHUB_WORKSPACE"))
    final, missing = plan_destination(workspace, require(env, "INPUT_DESTINATION"))
    tar_path, manifest_path = check_download(require(env, "DOWNLOAD_DIR"))
    manifest, digest = verify_download(env, tar_path, manifest_path)
    members = read_members(tar_path)
    files = scan_members(members)
    match_manifest(manifest, files)
    create_destination(workspace, final, missing, created)
    extract(tar_path, final, members)
    restore_link_targets(final, files)
    restore_modes(final, files)
    count = verify_tree(as_restored(manifest), final)
    drop_owner_read(final, files)
    return {
        "destination": final,
        "artifact-name": manifest["artifact_name"],
        "digest": digest,
        "file-count": str(count),
    }


def report(env: Mapping[str, str], output: str, outputs: Mapping[str, str]) -> None:
    """Write the step outputs and the step summary line."""
    write_outputs(output, outputs)
    name, count, digest = (
        outputs[key] for key in ("artifact-name", "file-count", "digest")
    )
    line = f"Unpacked `{name}`: {count} files, digest `{digest}` ✅"
    summary = env.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            _ = handle.write(line + "\n")
    print(line)


def run(env: Mapping[str, str]) -> int:
    """Run the unpack step; on any refusal remove what it created."""
    created: list[str] = []
    try:
        output = require(env, "GITHUB_OUTPUT")
        outputs = unpack(env, created)
        try:
            report(env, output, outputs)
        except OSError as error:
            fail(f"cannot write the step outputs or summary: {error.strerror}")
    except ManifestError:
        for problem in remove_created(created):
            print(f"::error::{escape_annotation(problem)}")
        return 1
    except BaseException:
        # An unforeseen error must not leave a partial tree behind either.
        for problem in remove_created(created):
            print(f"::error::{escape_annotation(problem)}")
        raise
    return 0


def main() -> int:
    """Run the unpack step against the process environment."""
    return run(os.environ)


if __name__ == "__main__":
    sys.exit(main())
