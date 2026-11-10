# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Pins on what the 'overlay' option of 'unpack' mode adds.

Rollback and the mode updates after the move recognise an entry this
step added by the device and inode it had in staging. A filesystem may
give a freed inode's number to the next entry it creates, so an entry
another process unlinked and recreated could pass for the one this
step added. Every entry is therefore kept alive from staging until the
step ends: a file or symlink by a hard link in a private pin directory,
and a directory, which cannot be hard linked, by an open descriptor,
through which rollback can also give its owner access back.
"""

from __future__ import annotations

import os
import resource
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field

from scripts.manifest import open_parent
from scripts.manifest_schema import fail
from scripts.unpack_anchor import Anchor
from scripts.unpack_modes import Identity
from scripts.unpack_rollback import DIRECTORY_FLAGS, Added, remove_held, warn

PINS_PREFIX = ".build-artifact-pins-"
# Descriptors left for everything else the step opens.
SPARE_DESCRIPTORS = 64


@dataclass
class Pins:
    """The pin directory in the working directory, and each directory held.

    name and fd are the pin directory's, once open makes it; fds maps
    each directory's identity to its descriptor. limit is the open-file
    limit reserve found, to put back when the pins go.
    """

    name: str | None = None
    fd: int | None = None
    fds: dict[Identity, int] = field(default_factory=dict)
    count: int = 0
    limit: tuple[int, int] | None = None

    def open(self, name: str, fd: int) -> None:
        """Hold name, a fresh private directory in '.' open as fd, for the hard links."""
        self.name = name
        self.fd = fd

    def reserve(self, count: int) -> None:
        """Raise the soft open-file limit to hold count more directories.

        Fails, before anything is added, when the hard limit is too low.
        """
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = len(self.fds) + count + SPARE_DESCRIPTORS
        if soft == resource.RLIM_INFINITY or wanted <= soft:
            return
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        except (OSError, ValueError):
            fail(
                f"destination: adding {count} directories needs {wanted} "
                + f"open files, beyond the limit of {hard}"
            )
        if self.limit is None:
            self.limit = (soft, hard)

    def pin(self, staging: str, records: Iterable[Added]) -> None:
        """Keep each entry records name in staging alive until release."""
        if self.fd is None:
            fail("destination: no pin directory is open")
        for entry in records:
            parent, name = open_parent(staging, entry.relative)
            try:
                if entry.directory:
                    self.hold(name, parent, entry)
                else:
                    self.count += 1
                    os.link(
                        name,
                        str(self.count),
                        src_dir_fd=parent,
                        dst_dir_fd=self.fd,
                        follow_symlinks=False,
                    )
            except OSError as error:
                fail(f"{entry.relative}: cannot pin in staging: {error.strerror}")
            finally:
                os.close(parent)

    def hold(self, name: str, parent: int, entry: Added) -> None:
        """Hold directory name in parent open, if it is the one entry records."""
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (entry.device, entry.inode) or not (
            stat.S_ISDIR(info.st_mode)
        ):
            os.close(fd)
            fail(f"{entry.relative}: changed in staging")
        self.fds[(entry.device, entry.inode)] = fd

    def release(self, anchor: Anchor) -> list[str]:
        """Close every descriptor, remove the pin directory, restore the limit.

        anchor holds the destination, which holds the pin directory. The
        pin directory is emptied through its own descriptor, and removed
        only while its name still names it, so a directory another
        process put in its place keeps its contents, with a warning.
        Returns an error message if the pin directory stays.
        """
        problems: list[str] = []
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()
        if self.name is not None and self.fd is not None:
            try:
                with anchor.entered():
                    if not remove_held(None, self.name, self.fd):
                        warn(f"left {self.name}: another process replaced it")
            except OSError as error:
                problems.append(f"cannot remove {self.name}: {error.strerror}")
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.name = None
        if self.limit is not None:
            resource.setrlimit(resource.RLIMIT_NOFILE, self.limit)
            self.limit = None
        return problems
