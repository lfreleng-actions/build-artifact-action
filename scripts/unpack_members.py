# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The checks 'unpack' mode runs on every tar member before extracting any.

scan_members refuses a member whose path, type or place in the tree
unpack does not accept, and match_manifest a set of members that
differs from the manifest's entries.
"""

from __future__ import annotations

import tarfile
from collections.abc import Mapping
from typing import cast

from scripts.manifest import Manifest
from scripts.manifest_schema import RegularEntry, fail, path_problem
from scripts.unpack_links import check_link_target

# Verifying and removing a tree recurse once per directory level, so a
# bound far inside Python's recursion limit keeps both from failing.
MAX_DEPTH = 256


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
