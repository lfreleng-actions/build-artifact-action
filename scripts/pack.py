# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The 'pack' mode of build-artifact-action (not implemented yet)."""

import sys


def main() -> int:
    """Report that 'pack' mode is not implemented and fail."""
    print("::error::pack: not implemented")
    return 1


if __name__ == "__main__":
    sys.exit(main())
