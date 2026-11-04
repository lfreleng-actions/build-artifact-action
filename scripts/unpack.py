# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The 'unpack' mode of build-artifact-action (not implemented yet)."""

import sys


def main() -> int:
    """Report that 'unpack' mode is not implemented and fail."""
    print("::error::unpack: not implemented")
    return 1


if __name__ == "__main__":
    sys.exit(main())
