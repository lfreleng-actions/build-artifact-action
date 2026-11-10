# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Rollback for the 'overlay' option of build-artifact-action's 'unpack' mode.

An overlay unpack records each entry it adds to the destination with
the identity it had in staging. On failure, rollback first renames
each entry into a private quarantine directory, then removes it there
only if it has that identity, and a directory only with rmdir, so
nothing another process put in the destination in the meantime is
removed. Each entry stays pinned until rollback ends (unpack_pins.py),
so no other entry can take its identity.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass

from scripts.manifest import open_dir, open_parent
from scripts.manifest_schema import ManifestError, escape_annotation
from scripts.unpack_modes import Identity
from scripts.unpack_rename import rename_noreplace

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
QUARANTINE_PREFIX = ".build-artifact-rollback-"


@dataclass(frozen=True)
class Added:
    """An entry an overlay unpack created, and the identity it had then.

    Rollback removes the entry only while it still has that identity,
    so an entry another process put in its place survives. staging
    marks the private staging directory, which rollback removes whole.
    """

    root: str
    relative: str
    device: int
    inode: int
    directory: bool
    staging: bool = False


def make_private_dir(dir_fd: int | None, prefix: str) -> str:
    """Create a fresh directory only its owner can enter in dir_fd; return its name.

    The name is relative, unlike tempfile.mkdtemp's absolute path, so
    nothing reaches the new directory through a path above dir_fd, or
    above the working directory when dir_fd is None. Raises OSError.
    """
    while True:
        name = prefix + secrets.token_hex(8)
        try:
            os.mkdir(name, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            continue
        return name


def staging_entry(root: str, staging: str, fd: int) -> Added:
    """Return the record of the staging directory make_staging created, held as fd."""
    info = os.fstat(fd)
    relative = os.path.basename(staging)
    return Added(root, relative, info.st_dev, info.st_ino, True, staging=True)


def warn(message: str) -> None:
    """Print message as one '::warning::' line."""
    print(f"::warning::{escape_annotation(message)}")


def clear_held(fd: int) -> None:
    """Empty the directory fd holds, never resolving a pathname above it.

    The walk starts at fd, and each entry is opened, given its owner
    access and removed relative to its parent's descriptor, so renaming
    the directory, or any above it, cannot redirect the removal. A
    directory whose mode denies its owner access is changed without
    following a symlink; one that cannot be is left, and removing its
    parent then fails. Raises OSError.
    """
    for _, dirnames, _, dir_fd in os.fwalk(os.curdir, dir_fd=fd):
        for name in dirnames:
            with contextlib.suppress(OSError, NotImplementedError, ValueError):
                os.chmod(name, stat.S_IRWXU, dir_fd=dir_fd, follow_symlinks=False)
    for _, dirnames, filenames, dir_fd in os.fwalk(os.curdir, topdown=False, dir_fd=fd):
        for name in filenames:
            os.unlink(name, dir_fd=dir_fd)
        for name in dirnames:
            # fwalk lists a symlink to a directory here, without entering it.
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                os.rmdir(name, dir_fd=dir_fd)
            else:
                os.unlink(name, dir_fd=dir_fd)


def remove_held(parent: int | None, name: str, fd: int) -> bool:
    """Empty the directory fd holds, then remove name in parent if it is that one.

    parent is a directory descriptor, or None for the working directory.
    Returns False, leaving name, when name no longer names the directory
    fd holds: another process moved it. Whatever took its place keeps
    its contents, and rmdir could remove it only while it is empty.
    Raises OSError.
    """
    clear_held(fd)
    held = os.fstat(fd)
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
        return False
    os.rmdir(name, dir_fd=parent)
    return True


def same_entry(info: os.stat_result, entry: Added) -> bool:
    """Return whether info describes the entry recorded in entry."""
    return (info.st_dev, info.st_ino) == (entry.device, entry.inode) and (
        stat.S_ISDIR(info.st_mode) == entry.directory
    )


def open_up_held(added: list[Added], held: Mapping[Identity, int]) -> None:
    """Give the owner full access to each directory this step created.

    held maps the identity of each directory pinned in staging to its
    descriptor (unpack_pins.py), which reaches the directory even when
    its mode denies its owner read, so it can no longer be opened by
    name. Its staged mode may deny the owner write access, without
    which rollback could not remove what the step put inside it.
    """
    ours = {(entry.device, entry.inode) for entry in added if entry.directory}
    for identity, fd in held.items():
        if identity in ours:
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o700)


@dataclass
class Quarantine:
    """A private directory, made in root, that rollback isolates entries in.

    An entry is renamed in under a fresh name before its identity is
    checked, so nothing can take its place between the check and the
    removal: whatever another process puts at the old path stays there.
    """

    path: str
    fd: int
    count: int = 0

    def slot(self) -> str:
        """Return a fresh name in the quarantine."""
        self.count += 1
        return str(self.count)


def remove_staging(entry: Added, quarantine: Quarantine, slot: str) -> str | None:
    """Remove the staging directory isolated as slot; return why it stayed, or None.

    Everything happens relative to the quarantine's descriptor and the
    staging directory's own, never through the quarantine's path, which
    another process could rename and put a directory of its own at.
    """
    fd = os.open(slot, DIRECTORY_FLAGS, dir_fd=quarantine.fd)
    try:
        if not same_entry(os.fstat(fd), entry) or not remove_held(
            quarantine.fd, slot, fd
        ):
            return "another process replaced it"
    finally:
        os.close(fd)
    return None


def remove_isolated(entry: Added, quarantine: Quarantine, slot: str) -> str | None:
    """Remove the isolated entry; return why it stayed, or None."""
    try:
        if entry.staging:
            return remove_staging(entry, quarantine, slot)
        if entry.directory:
            os.rmdir(slot, dir_fd=quarantine.fd)
        else:
            os.unlink(slot, dir_fd=quarantine.fd)
    except OSError as error:
        return error.strerror
    return None


def remove_entry(entry: Added, quarantine: Quarantine) -> str | None:
    """Remove one entry this step created; return why it stayed, or None.

    The entry is renamed into the quarantine and removed there only if
    it still has the recorded identity, and a directory only with
    rmdir. Otherwise it goes back with rename_noreplace, so neither an
    entry another process put in its place nor anything inside it is
    removed, and none written at the old path meanwhile is replaced.
    """
    path = os.path.normpath(os.path.join(entry.root, entry.relative))
    parent, name = open_parent(entry.root, entry.relative)
    try:
        slot = quarantine.slot()
        try:
            os.rename(name, slot, src_dir_fd=parent, dst_dir_fd=quarantine.fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            return f"cannot remove {path}: {error.strerror}"
        if not same_entry(
            os.stat(slot, dir_fd=quarantine.fd, follow_symlinks=False), entry
        ):
            problem = f"left {path} in place: another process replaced it"
        else:
            reason = remove_isolated(entry, quarantine, slot)
            if reason is None:
                return None
            problem = f"cannot remove {path}: {reason}"
        try:
            rename_noreplace(quarantine.fd, slot, parent, name)
        except OSError:
            return f"{problem}; it is now {os.path.join(quarantine.path, slot)}"
        return problem
    finally:
        os.close(parent)


def open_quarantine(root: str) -> Quarantine:
    """Make a private quarantine directory in root and open it."""
    parent = open_dir(root, f"root {root}", None)
    try:
        name = make_private_dir(parent, QUARANTINE_PREFIX)
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    return Quarantine(os.path.normpath(os.path.join(root, name)), fd)


def close_quarantine(root: str, quarantine: Quarantine, problems: list[str]) -> None:
    """Remove the quarantine in root if it is still there and empty, then close it.

    It is removed only while its name in root still names the directory
    its descriptor holds, so a directory put in its place stays.
    """
    try:
        parent = open_dir(root, f"root {root}", None)
        try:
            name = os.path.basename(quarantine.path)
            held = os.fstat(quarantine.fd)
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
                warn(f"left {quarantine.path}: another process replaced it")
            else:
                os.rmdir(name, dir_fd=parent)
        finally:
            os.close(parent)
    except (OSError, ManifestError):
        problems.append(f"left {quarantine.path} holding what it could not restore")
    finally:
        os.close(quarantine.fd)


def remove_all(added: list[Added], problems: list[str]) -> None:
    """Remove each entry in added, newest first, through a quarantine."""
    quarantines: dict[str, Quarantine] = {}
    try:
        for entry in reversed(added):
            path = os.path.normpath(os.path.join(entry.root, entry.relative))
            try:
                if entry.root not in quarantines:
                    quarantines[entry.root] = open_quarantine(entry.root)
                problem = remove_entry(entry, quarantines[entry.root])
            except ManifestError:
                problem = f"cannot remove {path}: a parent is no longer reachable"
            except OSError as error:
                problem = f"cannot remove {path}: {error.strerror}"
            if problem:
                problems.append(problem)
    finally:
        for root, quarantine in quarantines.items():
            close_quarantine(root, quarantine, problems)


def remove_added(
    added: list[Added], held: Mapping[Identity, int] | None = None
) -> list[str]:
    """Remove each entry an overlay unpack added, newest first.

    held maps the identity of each directory pinned to its descriptor,
    which the caller closes. Returns an error message for each entry
    left behind.
    """
    problems: list[str] = []
    open_up_held(added, {} if held is None else held)
    remove_all(added, problems)
    return problems
