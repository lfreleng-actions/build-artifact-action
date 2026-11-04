# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/validate.py."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts.validate import ValidationError, run, validate

DIGEST = "0123456789abcdef" * 4


class ValidateTestCase(unittest.TestCase):
    """Run validate() against a throwaway GITHUB_WORKSPACE."""

    _base: Path | None = None

    @property
    def base(self) -> Path:
        """Return a temporary directory holding the workspace."""
        if self._base is None:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            self._base = Path(os.path.realpath(tmp.name))
            (self._base / "workspace").mkdir()
        return self._base

    @property
    def workspace(self) -> Path:
        """Return the GITHUB_WORKSPACE for this test."""
        return self.base / "workspace"

    def env(self, mode: str, **inputs: str) -> dict[str, str]:
        """Build the step environment the action passes to the script."""
        env = {"GITHUB_WORKSPACE": str(self.workspace), "INPUT_MODE": mode}
        env["INPUT_ARTIFACT_NAME"] = inputs.pop("artifact_name", "my-artifact")
        for key, value in inputs.items():
            env["INPUT_" + key.upper()] = value
        return env

    def assert_rejected(self, env: dict[str, str], name: str) -> None:
        """Assert that validation fails with an error naming the input."""
        with self.assertRaises(ValidationError) as caught:
            _ = validate(env)
        self.assertTrue(
            any(error.startswith(name + ":") for error in caught.exception.errors),
            caught.exception.errors,
        )


class AcceptedTest(ValidateTestCase):
    """Every accepted value in the interface table."""

    @unittest.expectedFailure
    def test_pack_defaults(self) -> None:
        out = validate(self.env("pack"))
        self.assertEqual(out["mode"], "pack")
        self.assertEqual(out["artifact_name"], "my-artifact")
        self.assertEqual(out["root"], str(self.workspace))
        self.assertEqual(out["paths"], "")
        self.assertEqual(out["retention_days"], "1")
        self.assertEqual(out["fail_on_empty"], "true")
        self.assertEqual(out["overwrite"], "false")

    @unittest.expectedFailure
    def test_artifact_names(self) -> None:
        for name in ("a", "A.b_c-9", "..dots..", "release-1.2.3"):
            with self.subTest(name=name):
                out = validate(self.env("pack", artifact_name=name))
                self.assertEqual(out["artifact_name"], name)

    @unittest.expectedFailure
    def test_pack_path(self) -> None:
        for path in (".", "build", "build/out", ".hidden/dir_1"):
            with self.subTest(path=path):
                out = validate(self.env("pack", path=path))
                self.assertEqual(out["root"], os.path.normpath(self.workspace / path))

    @unittest.expectedFailure
    def test_pack_paths(self) -> None:
        out = validate(self.env("pack", path="build", paths="dist\n lib/a.txt \n\n"))
        self.assertEqual(out["paths"], "dist\nlib/a.txt")

    @unittest.expectedFailure
    def test_retention_days(self) -> None:
        for days in ("1", "45", "90"):
            with self.subTest(days=days):
                out = validate(self.env("pack", retention_days=days))
                self.assertEqual(out["retention_days"], days)

    @unittest.expectedFailure
    def test_retention_days_leading_zeros(self) -> None:
        for days, normalised in (("01", "1"), ("090", "90"), ("0" * 5000 + "7", "7")):
            with self.subTest(days=days[-8:]):
                out = validate(self.env("pack", retention_days=days))
                self.assertEqual(out["retention_days"], normalised)

    @unittest.expectedFailure
    def test_booleans(self) -> None:
        for value in ("true", "false"):
            with self.subTest(value=value):
                out = validate(self.env("pack", fail_on_empty=value, overwrite=value))
                self.assertEqual(out["fail_on_empty"], value)
                self.assertEqual(out["overwrite"], value)

    @unittest.expectedFailure
    def test_unpack(self) -> None:
        out = validate(
            self.env(
                "unpack",
                destination="out/dir",
                artifact_id="12345",
                expected_digest=DIGEST,
            )
        )
        self.assertEqual(out["mode"], "unpack")
        self.assertEqual(out["root"], str(self.workspace / "out" / "dir"))
        self.assertEqual(out["artifact_id"], "12345")
        self.assertEqual(out["expected_digest"], DIGEST)

    @unittest.expectedFailure
    def test_unpack_into_workspace(self) -> None:
        out = validate(self.env("unpack", destination="."))
        self.assertEqual(out["root"], str(self.workspace))

    @unittest.expectedFailure
    def test_unpack_ignores_pack_defaults(self) -> None:
        env = self.env(
            "unpack",
            destination="out",
            path=".",
            paths="",
            retention_days="1",
            fail_on_empty="true",
            overwrite="false",
        )
        self.assertEqual(validate(env)["root"], str(self.workspace / "out"))

    @unittest.expectedFailure
    def test_symlink_beside_target(self) -> None:
        (self.workspace / "link").symlink_to(self.base)
        out = validate(self.env("unpack", destination="out"))
        self.assertEqual(out["root"], str(self.workspace / "out"))

    @unittest.expectedFailure
    def test_workspace_through_symlink(self) -> None:
        (self.workspace / "build").mkdir()
        (self.base / "ws-link").symlink_to(self.workspace)
        env = self.env("pack", path="build")
        env["GITHUB_WORKSPACE"] = str(self.base / "ws-link")
        self.assertEqual(validate(env)["root"], str(self.workspace / "build"))
        env = self.env("unpack", destination="out/sub")
        env["GITHUB_WORKSPACE"] = str(self.base / "ws-link")
        self.assertEqual(validate(env)["root"], str(self.workspace / "out" / "sub"))


class RejectedTest(ValidateTestCase):
    """Every rejected shape fails with an error naming the input."""

    @unittest.expectedFailure
    def test_mode(self) -> None:
        for mode in ("", "Pack", "PACK", "upload", "pack "):
            with self.subTest(mode=mode):
                self.assert_rejected(self.env(mode), "mode")

    @unittest.expectedFailure
    def test_artifact_name(self) -> None:
        for name in ("", "bad name", "a/b", "a\nb", "naïve", "a:b"):
            with self.subTest(name=name):
                self.assert_rejected(
                    self.env("pack", artifact_name=name), "artifact-name"
                )

    @unittest.expectedFailure
    def test_artifact_id(self) -> None:
        for value in ("abc", "-1", "1.0", " 1", "1\n2"):
            with self.subTest(value=value):
                self.assert_rejected(
                    self.env("unpack", destination="out", artifact_id=value),
                    "artifact-id",
                )

    @unittest.expectedFailure
    def test_path_grammar(self) -> None:
        for path in ("", "..", "a/../b", "a/..", "/abs", "a//b", "a/", "a b"):
            with self.subTest(path=path):
                self.assert_rejected(self.env("pack", path=path), "path")

    @unittest.expectedFailure
    def test_paths_grammar(self) -> None:
        for paths in ("a\n..", "../x", "/etc/passwd", "ok\nbad entry"):
            with self.subTest(paths=paths):
                self.assert_rejected(self.env("pack", paths=paths), "paths")

    @unittest.expectedFailure
    def test_destination_grammar(self) -> None:
        for destination in ("..", "out/../..", "/tmp/out", "out/"):
            with self.subTest(destination=destination):
                self.assert_rejected(
                    self.env("unpack", destination=destination), "destination"
                )

    @unittest.expectedFailure
    def test_destination_required(self) -> None:
        self.assert_rejected(self.env("unpack"), "destination")
        self.assert_rejected(self.env("unpack", destination=""), "destination")

    @unittest.expectedFailure
    def test_symlink_escaping_workspace(self) -> None:
        (self.workspace / "escape").symlink_to(self.base)
        self.assert_rejected(self.env("pack", path="escape"), "path")
        self.assert_rejected(
            self.env("unpack", destination="escape/sub"), "destination"
        )

    @unittest.expectedFailure
    def test_symlink_inside_workspace(self) -> None:
        (self.workspace / "real").mkdir()
        (self.workspace / "inner").symlink_to(self.workspace / "real")
        self.assert_rejected(self.env("pack", path="inner"), "path")
        self.assert_rejected(self.env("unpack", destination="inner/sub"), "destination")

    @unittest.expectedFailure
    def test_symlink_intermediate_directory(self) -> None:
        (self.workspace / "a" / "real").mkdir(parents=True)
        (self.workspace / "a" / "link").symlink_to(self.workspace / "a" / "real")
        self.assert_rejected(self.env("pack", path="a/link/b"), "path")
        self.assert_rejected(
            self.env("unpack", destination="a/link/b/c"), "destination"
        )

    @unittest.expectedFailure
    def test_non_directory_component(self) -> None:
        _ = (self.workspace / "out").write_text("not a directory\n", encoding="utf-8")
        for value in ("out", "out/sub", "out/sub/deeper"):
            with self.subTest(value=value):
                self.assert_rejected(self.env("pack", path=value), "path")
                self.assert_rejected(
                    self.env("unpack", destination=value), "destination"
                )

    @unittest.expectedFailure
    def test_paths_symlink_escaping_root(self) -> None:
        (self.workspace / "build").mkdir()
        (self.workspace / "build" / "up").symlink_to(self.base)
        self.assert_rejected(self.env("pack", path="build", paths="up"), "paths")

    @unittest.expectedFailure
    def test_booleans(self) -> None:
        for value in ("", "True", "TRUE", "yes", "1", "false "):
            with self.subTest(value=value):
                self.assert_rejected(
                    self.env("pack", fail_on_empty=value), "fail-on-empty"
                )
                self.assert_rejected(self.env("pack", overwrite=value), "overwrite")

    @unittest.expectedFailure
    def test_retention_days(self) -> None:
        oversized = ("1" + "0" * 5000, "9" * 5000, "0" * 5000)
        for days in ("0", "91", "", "-1", "1.5", "ten", "100", *oversized):
            with self.subTest(days=days[:8]):
                self.assert_rejected(
                    self.env("pack", retention_days=days), "retention-days"
                )

    @unittest.expectedFailure
    def test_expected_digest(self) -> None:
        for digest in (DIGEST[:-1], DIGEST + "0", DIGEST.upper(), "g" * 64):
            with self.subTest(digest=digest):
                self.assert_rejected(
                    self.env("unpack", destination="out", expected_digest=digest),
                    "expected-digest",
                )

    @unittest.expectedFailure
    def test_unpack_inputs_with_pack(self) -> None:
        cases = {
            "destination": {"destination": "out"},
            "artifact-id": {"artifact_id": "1"},
            "expected-digest": {"expected_digest": DIGEST},
        }
        for name, inputs in cases.items():
            with self.subTest(name=name):
                self.assert_rejected(self.env("pack", **inputs), name)

    @unittest.expectedFailure
    def test_pack_inputs_with_unpack(self) -> None:
        cases = {
            "path": {"path": "build"},
            "paths": {"paths": "a"},
            "retention-days": {"retention_days": "5"},
            "fail-on-empty": {"fail_on_empty": "false"},
            "overwrite": {"overwrite": "true"},
        }
        for name, inputs in cases.items():
            with self.subTest(name=name):
                self.assert_rejected(
                    self.env("unpack", destination="out", **inputs), name
                )

    @unittest.expectedFailure
    def test_workspace_unset(self) -> None:
        env = self.env("pack")
        del env["GITHUB_WORKSPACE"]
        self.assert_rejected(env, "GITHUB_WORKSPACE")

    @unittest.expectedFailure
    def test_reports_every_error(self) -> None:
        env = self.env("pack", artifact_name="bad name", retention_days="0")
        with self.assertRaises(ValidationError) as caught:
            _ = validate(env)
        self.assertEqual(len(caught.exception.errors), 2)


class RunTest(ValidateTestCase):
    """run() writes outputs only when every input is valid."""

    def output_file(self) -> Path:
        """Return an empty GITHUB_OUTPUT file."""
        path = self.base / "output"
        path.touch()
        return path

    @unittest.expectedFailure
    def test_writes_heredoc_outputs(self) -> None:
        output = self.output_file()
        env = self.env("pack", paths="a\nb")
        env["GITHUB_OUTPUT"] = str(output)
        self.assertEqual(run(env), 0)
        lines = output.read_text(encoding="utf-8").splitlines()
        values: dict[str, list[str]] = {}
        while lines:
            key, delimiter = lines.pop(0).split("<<", 1)
            end = lines.index(delimiter)
            values[key], lines = lines[:end], lines[end + 1 :]
        self.assertEqual(values["mode"], ["pack"])
        self.assertEqual(values["paths"], ["a", "b"])
        self.assertEqual(values["root"], [str(self.workspace)])

    @unittest.expectedFailure
    def test_failure_writes_nothing(self) -> None:
        output = self.output_file()
        env = self.env("bogus")
        env["GITHUB_OUTPUT"] = str(output)
        self.assertEqual(run(env), 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    _ = unittest.main()
