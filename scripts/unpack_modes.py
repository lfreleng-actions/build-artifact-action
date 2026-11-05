# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""File modes for the 'unpack' mode of build-artifact-action.

The 'data' filter extracts regular files owner-readable and writable,
without group or other write and without setuid, setgid or sticky.
These helpers put the tar's modes back within 0777, keep owner read
while the tree is verified, and clear it only afterwards, so a file
whose mode denies its owner read can still be hashed.
"""

from __future__ import annotations

import copy
import os
import stat
import tarfile
from collections.abc import Mapping

from scripts.manifest import Manifest, open_parent, open_regular
from scripts.manifest_schema import fail

# The 'data' filter drops setuid, setgid and sticky, and unpack never
# puts them back.
SAFE_MODE = 0o777


def open_extracted(root: str, name: str) -> int:
    """Open the regular file root/name for reading, following no link."""
    parent, base = open_parent(root, name)
    try:
        fd, _ = open_regular(base, follow=False, dir_fd=parent, label=name)
    finally:
        os.close(parent)
    return fd


def restore_modes(root: str, files: Mapping[str, tarfile.TarInfo]) -> None:
    """Set each regular file's mode from the tar, within 0777, owner-readable.

    The 'data' filter clears group and other write bits, which the
    manifest records, so they are put back without following a link.
    Owner read stays set until verify_tree has hashed the file;
    drop_owner_read clears it afterwards where the tar asks.
    """
    for name, member in files.items():
        if member.issym():
            continue
        fd = open_extracted(root, name)
        try:
            os.fchmod(fd, (member.mode & SAFE_MODE) | stat.S_IRUSR)
        except OSError as error:
            fail(f"{name}: cannot set mode: {error.strerror}")
        finally:
            os.close(fd)


def as_restored(manifest: Manifest) -> Manifest:
    """Return manifest with each mode as restore_modes leaves it.

    The digest-bearing manifest is left as it is; only this copy, which
    verify_tree checks the tree against, drops the bits unpack never
    sets and adds the owner read restore_modes keeps.
    """
    restored = copy.deepcopy(manifest)
    for entry in restored["files"]:
        if "symlink" in entry:
            continue
        mode = (int(entry["mode"], 8) & SAFE_MODE) | stat.S_IRUSR
        entry["mode"] = f"{mode:04o}"
    return restored


def stat_extracted(root: str, name: str) -> os.stat_result:
    """Return the lstat of root/name, which needs no read permission on it."""
    parent, base = open_parent(root, name)
    try:
        return os.stat(base, dir_fd=parent, follow_symlinks=False)
    except OSError as error:
        fail(f"{name}: cannot stat: {error.strerror}")
    finally:
        os.close(parent)


def drop_owner_read(root: str, files: Mapping[str, tarfile.TarInfo]) -> None:
    """Clear owner read where the tar's mode lacks it, then check each mode.

    Each inode is opened, changed and checked once, one descriptor at a
    time. A hard link to an inode already changed can no longer be
    opened for reading, so it is found by its inode through lstat, and
    its mode checked there; the pre-scan made its mode the target's.
    """
    done: set[tuple[int, int]] = set()
    for name, member in files.items():
        if member.issym() or member.mode & stat.S_IRUSR:
            continue
        mode = member.mode & SAFE_MODE
        info = stat_extracted(root, name)
        if (info.st_dev, info.st_ino) not in done:
            fd = open_extracted(root, name)
            try:
                os.fchmod(fd, mode)
                info = os.fstat(fd)
            except OSError as error:
                fail(f"{name}: cannot set mode: {error.strerror}")
            finally:
                os.close(fd)
            done.add((info.st_dev, info.st_ino))
        actual = stat.S_IMODE(info.st_mode)
        if actual != mode:
            fail(f"{name}: mode {actual:04o} is not {mode:04o}")
