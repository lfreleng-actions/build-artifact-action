# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Symlink targets for the 'unpack' mode of build-artifact-action.

A symlink target is kept as the tar stores it, so a tree round-trips
with spellings such as './bin/run.sh'. Newer 'data' filters normalise
link targets as they extract, so unpack puts the stored spelling back
before the tree is verified against the manifest.
"""

from __future__ import annotations

import os
import tarfile
from collections.abc import Mapping

from scripts.manifest import open_parent
from scripts.manifest_schema import fail


def check_link_target(name: str, target: str) -> None:
    """Fail unless symlink name, pointing at target, stays inside the root.

    '.' and empty segments change nothing and are allowed. '..' is
    allowed only before the first named segment, where it climbs through
    the link's own parent directories, which are real directories; after
    a named segment, which may itself be a symlink, it could climb out.
    The target then stays inside exactly when it climbs no higher than
    the link's own depth.
    """
    if not target:
        fail(f"tar member {name!r}: symlink target is empty")
    if "\0" in target:
        fail(f"tar member {name!r}: symlink target contains a NUL byte")
    if target.startswith("/"):
        fail(f"tar member {name!r}: symlink target is absolute")
    segments = [segment for segment in target.split("/") if segment not in ("", ".")]
    climbs = 0
    while climbs < len(segments) and segments[climbs] == "..":
        climbs += 1
    if ".." in segments[climbs:]:
        fail(f"tar member {name!r}: symlink target has '..' after a named segment")
    if climbs > name.count("/"):
        fail(f"tar member {name!r}: symlink target leaves the root")


def restore_link_targets(root: str, files: Mapping[str, tarfile.TarInfo]) -> None:
    """Give back each symlink the spelling the tar stores, where it was normalised.

    Only a link the extraction rewrote to the normalised form of its
    stored target is replaced; any other difference is left for
    verify_tree to report.
    """
    for name, member in files.items():
        stored = member.linkname
        normalised = os.path.normpath(stored)
        if not member.issym() or normalised == stored:
            continue
        parent, base = open_parent(root, name)
        try:
            if os.readlink(base, dir_fd=parent) == normalised:
                os.unlink(base, dir_fd=parent)
                os.symlink(stored, base, dir_fd=parent)
        except OSError as error:
            fail(f"{name}: cannot restore symlink target: {error.strerror}")
        finally:
            os.close(parent)
