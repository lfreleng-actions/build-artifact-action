# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""An atomic rename that never replaces its destination.

os.rename silently replaces a file, or an empty directory, at its
destination, and the standard library offers no rename that refuses
to. The overlay unpack must never replace an entry another process
created, so it moves entries with the kernel's own no-replace rename,
reached through ctypes: renameat2 with RENAME_NOREPLACE on Linux and
renameatx_np with RENAME_EXCL on macOS. The check and the move are
then one system call, which fails with EEXIST rather than replace.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from collections.abc import Callable
from typing import cast

# <linux/fs.h> and macOS <sys/stdio.h>.
RENAME_NOREPLACE = 0x1
RENAME_EXCL = 0x4

RenameAt = Callable[[int, bytes, int, bytes, int], int]


def load() -> tuple[RenameAt, int] | None:
    """Return the C library's no-replace rename and its flag, or None."""
    name, flag = (
        ("renameatx_np", RENAME_EXCL)
        if sys.platform == "darwin"
        else ("renameat2", RENAME_NOREPLACE)
    )
    prototype = ctypes.CFUNCTYPE(
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
        use_errno=True,
    )
    try:
        function = prototype((name, ctypes.CDLL(None, use_errno=True)))
    except (OSError, AttributeError):
        return None
    return cast("RenameAt", function), flag


RENAME = load()


def rename_noreplace(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
    """Move src in src_dir_fd to dst in dst_dir_fd, never replacing dst.

    Raises FileExistsError when dst exists, and OSError for any other
    failure, ENOSYS included where the platform has no such rename.
    """
    if RENAME is None:
        raise OSError(errno.ENOSYS, "no atomic no-replace rename on this platform")
    function, flag = RENAME
    if function(src_dir_fd, os.fsencode(src), dst_dir_fd, os.fsencode(dst), flag):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), dst)
