# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the scripts/pack.py stub."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class StubTest(unittest.TestCase):
    """The pack mode fails with its 'not implemented' diagnostic."""

    def test_pack_not_implemented(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPTS / "pack.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "::error::pack: not implemented\n")


if __name__ == "__main__":
    _ = unittest.main()
