# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""File modes for the 'unpack' mode of build-artifact-action.

The 'data' filter extracts regular files owner-readable and writable,
without group or other write and without setuid, setgid or sticky.
These helpers put the tar's modes back within 0777, keep owner read
while the tree is verified, and clear it only afterwards, so a file
whose mode denies its owner read can still be hashed. The filter
also leaves directories at the umask's default mode, so their modes
from the tar are put back last, once nothing else needs to walk them.
"""

from __future__ import annotations

import copy
import os
import stat
import tarfile
from collections.abc import Iterable, Mapping

from scripts.manifest import Manifest, open_dir, open_parent, open_regular
from scripts.manifest_schema import fail

# The 'data' filter drops setuid, setgid and sticky, and unpack never
# puts them back.
SAFE_MODE = 0o777

# A file's (st_dev, st_ino).
Identity = tuple[int, int]


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


def check_identity(
    name: str, info: os.stat_result, expected: Mapping[str, Identity] | None
) -> None:
    """Fail unless info is the entry expected records for name.

    With overlay, the entries are already in the destination, where
    another process may have replaced one; expected holds the identity
    each had in staging, and no mode is set on anything else.
    """
    if expected is not None and expected.get(name) != (info.st_dev, info.st_ino):
        fail(f"{name}: replaced in the destination during unpack")


def drop_owner_read(
    root: str,
    files: Mapping[str, tarfile.TarInfo],
    expected: Mapping[str, Identity] | None = None,
) -> None:
    """Clear owner read where the tar's mode lacks it, then check each mode.

    Each inode is opened, changed and checked once, one descriptor at a
    time. A hard link to an inode already changed can no longer be
    opened for reading, so it is found by its inode through lstat, and
    its mode checked there; the pre-scan made its mode the target's.
    With expected, each entry must keep the identity recorded there.
    """
    done: set[tuple[int, int]] = set()
    for name, member in files.items():
        if member.issym() or member.mode & stat.S_IRUSR:
            continue
        mode = member.mode & SAFE_MODE
        info = stat_extracted(root, name)
        check_identity(name, info, expected)
        if (info.st_dev, info.st_ino) not in done:
            fd = open_extracted(root, name)
            try:
                check_identity(name, os.fstat(fd), expected)
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


def restore_dir_modes(
    root: str,
    members: Iterable[tarfile.TarInfo],
    expected: Mapping[str, Identity] | None = None,
) -> None:
    """Set each directory member's mode from the tar, within 0777, and check it.

    Runs after drop_owner_read, deepest directory first, so no mode set
    here can deny the walks before it or the opens of the directories
    still to set: each is opened through ancestors not yet changed.
    With expected, each directory must keep the identity recorded there.
    """
    directories = sorted(
        (member for member in members if member.isdir()),
        key=lambda member: member.name.count("/"),
        reverse=True,
    )
    for member in directories:
        name, mode = member.name, member.mode & SAFE_MODE
        parent, base = open_parent(root, name)
        try:
            fd = open_dir(base, name, parent)
        finally:
            os.close(parent)
        try:
            check_identity(name, os.fstat(fd), expected)
            os.fchmod(fd, mode)
            actual = stat.S_IMODE(os.fstat(fd).st_mode)
        except OSError as error:
            fail(f"{name}: cannot set mode: {error.strerror}")
        finally:
            os.close(fd)
        if actual != mode:
            fail(f"{name}: mode {actual:04o} is not {mode:04o}")


def open_up(top: str) -> None:
    """Give the owner full access to every directory under top, top down.

    restore_dir_modes can leave a directory its owner cannot list or
    write, which shutil.rmtree could not then empty. Nothing else writes
    the tree this step created, so a path lstat finds a directory is
    still one when chmod follows it. Best effort: a directory this
    cannot change is left for rmtree to report.
    """
    pending = [top]
    while pending:
        path = pending.pop()
        try:
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode):
                continue
            os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
            with os.scandir(path) as entries:
                pending.extend(
                    entry.path
                    for entry in entries
                    if entry.is_dir(follow_symlinks=False)
                )
        except OSError:
            continue
