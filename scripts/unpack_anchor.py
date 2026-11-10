# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The overlay destination of build-artifact-action's 'unpack' mode, held open.

plan_overlay opens the destination by walking down from the workspace
with O_NOFOLLOW, so the descriptor it returns names the directory it
checked. Every later step reaches the destination through that
descriptor alone, never through its path again: a parent of the
destination swapped for a symlink in the meantime cannot redirect a
write. tarfile and the tree walks take paths, so the overlay runs with
the destination as the working directory, entered with fchdir, and
names everything in it relative to '.'.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Generator

from scripts.manifest_schema import fail

# fchdir needs no read permission on the directory it returns to.
HOME_FLAGS = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY


def names_working_directory(path: str) -> bool:
    """Return whether absolute path reaches the working directory, following no symlink.

    Each component is opened from the one above with O_NOFOLLOW, so a
    path whose parent was swapped for a symlink, even one leading back
    to the same directory, does not count.
    """
    fd = os.open("/", HOME_FLAGS)
    try:
        for segment in path.split("/"):
            if segment in ("", "."):
                continue
            try:
                child = os.open(segment, HOME_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                return False
            os.close(fd)
            fd = child
        here, there = os.stat(os.curdir), os.fstat(fd)
        return (here.st_dev, here.st_ino) == (there.st_dev, there.st_ino)
    finally:
        os.close(fd)


class Anchor:
    """The working directory to return to and the destination, both held open."""

    def __init__(self) -> None:
        """Start holding nothing: without overlay, entered changes nothing."""
        self.fds: tuple[int, int] | None = None

    def hold(self, destination: int) -> None:
        """Keep destination, a directory descriptor, and the working directory."""
        try:
            home = os.open(os.curdir, HOME_FLAGS)
        except OSError as error:
            os.close(destination)
            fail(f"cannot open the working directory: {error.strerror}")
        self.fds = (home, destination)

    @contextlib.contextmanager
    def entered(self) -> Generator[None, None, None]:
        """Run the block with the held destination as the working directory."""
        if self.fds is None:
            yield
            return
        home, destination = self.fds
        os.fchdir(destination)
        try:
            yield
        finally:
            os.fchdir(home)

    def release(self) -> None:
        """Close both descriptors."""
        if self.fds is not None:
            for fd in self.fds:
                os.close(fd)
            self.fds = None
