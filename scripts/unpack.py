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
import stat
import sys
import tarfile
import warnings
from collections.abc import Mapping

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
from scripts.manifest_schema import escape_annotation, fail
from scripts.unpack_destination import (
    create_destination,
    plan_destination,
    remove_created,
)
from scripts.unpack_links import restore_link_targets
from scripts.unpack_members import match_manifest, scan_members
from scripts.unpack_modes import (
    as_restored,
    drop_owner_read,
    restore_dir_modes,
    restore_modes,
)
from scripts.validate import inside, write_outputs

TAR_NAME = "build-artifact.tar"
MANIFEST_NAME = "build-artifact.manifest.json"


def require(env: Mapping[str, str], key: str) -> str:
    """Return env[key], failing when it is unset or empty."""
    value = env.get(key, "")
    if not value:
        fail(f"{key}: must be set")
    return value


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
    restore_dir_modes(final, members)
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
