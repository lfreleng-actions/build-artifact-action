# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Write the reproducible tar of build-artifact-action's pack mode.

Members are written in the order given, with times fixed, owners
cleared and modes masked to 0777. Each regular file is streamed into
the tar through one descriptor and hashed on the way, so the recorded
SHA-256, size and mode describe exactly the bytes and header archived.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from typing import BinaryIO, NoReturn, final

from scripts.manifest import CHUNK_SIZE, open_parent, open_regular
from scripts.manifest_schema import fail

MODE_MASK = 0o777


@dataclass
class Member:
    """One tar member: a directory, a regular file or a symlink.

    mode is the masked mode the tar records for a regular file, taken
    from the descriptor its content is read through.
    """

    path: str
    info: os.stat_result
    target: str = ""
    sha256: str = ""
    size: int = 0
    mode: int = 0


@final
class HashingReader:
    """Read a file in chunks for tarfile, hashing what passes through."""

    def __init__(self, handle: BinaryIO) -> None:
        self.handle: BinaryIO = handle
        self.digest = hashlib.sha256()
        self.size: int = 0

    def read(self, size: int = CHUNK_SIZE, /) -> bytes:
        """Return up to size bytes, adding them to the running hash."""
        chunk = self.handle.read(size)
        self.digest.update(chunk)
        self.size += len(chunk)
        return chunk


def file_state(info: os.stat_result) -> tuple[int, ...]:
    """Return what changes when an entry is replaced, edited or chmodded.

    Device, inode and link count catch a replacement, a symlink's
    included; type, mode, size and the modification and change times
    catch an edit in place, a directory gaining or losing an entry
    included.
    """
    return (
        info.st_dev,
        info.st_ino,
        info.st_nlink,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def changed(path: str) -> NoReturn:
    """Fail because the entry at path changed after it was selected."""
    fail(f"{path}: changed while it was being packed")


def check_member(root: str, member: Member) -> None:
    """Fail unless member is still the entry selected, unchanged.

    It is looked up without following a symlink, through parents that
    are never symlinks, and must keep the identity and state recorded
    at selection, or the tar or the manifest could describe a stale or
    swapped entry.
    """
    parent, name = open_parent(root, member.path)
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as error:
        fail(f"{member.path}: cannot stat: {error.strerror}")
    finally:
        os.close(parent)
    if file_state(info) != file_state(member.info):
        changed(member.path)


def check_members(root: str, members: Iterable[Member]) -> None:
    """Fail unless every member is unchanged since selection."""
    for member in members:
        check_member(root, member)


def check_root_dir(root: str, before: os.stat_result) -> None:
    """Fail unless the packed root is unchanged since before was taken.

    Where the whole root is selected, an entry added to or removed from
    it would otherwise go unrecorded, since the root is no member.
    """
    try:
        info = os.lstat(root)
    except OSError as error:
        fail(f"{root}: cannot stat: {error.strerror}")
    if file_state(info) != file_state(before):
        changed(root)


def tar_info(member: Member, mtime: int) -> tarfile.TarInfo:
    """Return the normalised header for member, without a size."""
    info = tarfile.TarInfo(member.path)
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = stat.S_IMODE(member.info.st_mode) & MODE_MASK
    if stat.S_ISDIR(member.info.st_mode):
        info.type = tarfile.DIRTYPE
    elif stat.S_ISLNK(member.info.st_mode):
        info.type = tarfile.SYMTYPE
        info.linkname = member.target
    return info


def add_file(
    archive: tarfile.TarFile, root: str, member: Member, info: tarfile.TarInfo
) -> None:
    """Stream the regular file member into archive, recording its hash.

    The opened file must be the one selected, unchanged: the same
    identity and link count, or a replacement hard-linked to an outside
    file could be archived, and the same mode, size and times, or an
    edit since selection would pass unnoticed. It must still match once
    streamed, or its header and content could disagree.
    """
    parent, name = open_parent(root, member.path)
    try:
        fd, opened = open_regular(name, follow=False, dir_fd=parent, label=member.path)
    finally:
        os.close(parent)
    if file_state(opened) != file_state(member.info):
        os.close(fd)
        changed(member.path)
    info.size = opened.st_size
    info.mode = member.mode = stat.S_IMODE(opened.st_mode) & MODE_MASK
    with os.fdopen(fd, "rb") as handle:
        reader = HashingReader(handle)
        archive.addfile(info, reader)
        streamed = os.fstat(handle.fileno())
    if file_state(streamed) != file_state(opened):
        changed(member.path)
    member.sha256 = reader.digest.hexdigest()
    member.size = reader.size


def write_tar(root: str, members: Iterable[Member], tar_path: str, mtime: int) -> None:
    """Write members to a PAX tar at tar_path, one file at a time."""
    try:
        with tarfile.open(
            tar_path, "w", format=tarfile.PAX_FORMAT, encoding="utf-8"
        ) as archive:
            for member in members:
                info = tar_info(member, mtime)
                if stat.S_ISREG(member.info.st_mode):
                    add_file(archive, root, member, info)
                    continue
                check_member(root, member)
                archive.addfile(info)
    except (OSError, tarfile.TarError) as error:
        fail(f"{tar_path}: cannot write: {error}")
