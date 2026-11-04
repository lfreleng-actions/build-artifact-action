# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the scripts/pack.py and scripts/unpack.py stubs."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class StubTest(unittest.TestCase):
    """Each mode fails with its own 'not implemented' diagnostic."""

    @unittest.expectedFailure
    def test_modes_not_implemented(self) -> None:
        for mode in ("pack", "unpack"):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [sys.executable, "-I", str(SCRIPTS / f"{mode}.py")],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, f"::error::{mode}: not implemented\n")


if __name__ == "__main__":
    _ = unittest.main()
