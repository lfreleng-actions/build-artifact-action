# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Check how action.yaml retries a failed overwriting upload.

GitHub's artefact service can fail to list an artefact for a moment
after its upload, so an overwriting upload that follows another can
miss the old artefact and collide with it. action.yaml repeats the
upload behind 'continue-on-error' when 'overwrite' is true. These tests
read the steps out of action.yaml with tests/action_steps.py and walk
them the way the runner would for a given sequence of upload outcomes.
"""

from __future__ import annotations

import re
import unittest

from tests.action_steps import Run, Step, action_text, load_steps, section, walk

ATTEMPTS = 5
WAITS = [2, 4, 8, 16]
OVERWRITE = "steps.validate.outputs.overwrite"
UPLOAD = "actions/upload-artifact@"


def artifact_id_value() -> str:
    """Return the expression the 'artifact-id' output takes its value from."""
    outputs = action_text().partition("\noutputs:\n")[2].partition("\nruns:")[0]
    match = re.search(r"^  artifact-id:\n(.*?)(?=^  \S)", outputs, re.M | re.S)
    if match is None:
        raise AssertionError("action.yaml declares no 'artifact-id' output")
    return match.group(1).partition("value:")[2]


def upload_section() -> list[Step]:
    """Return the steps from the first upload attempt to unpack's first."""
    return section("upload", "scratch")


def simulate(overwrite: str, outcomes: list[str]) -> Run:
    """Walk the upload steps with the given upload outcomes, in order."""
    context = {
        "inputs.mode": "pack",
        OVERWRITE: overwrite,
        "steps.validate.outputs.artifact_id": "",
    }
    return walk(upload_section(), context, UPLOAD, outcomes)


def attempts() -> list[Step]:
    """Return the upload attempts in the order they appear."""
    return [step for step in upload_section() if step.calls(UPLOAD)]


class UploadRetryTest(unittest.TestCase):
    """An overwriting upload is retried; any other upload is not."""

    def test_parser_reads_steps(self) -> None:
        """The parser finds the steps and their 'with' entries."""
        steps = {step.id: step for step in load_steps() if step.id}
        self.assertIn("validate", steps)
        self.assertEqual(steps["upload"].maps["with"]["compression-level"], "0")
        self.assertIn("pack.py", steps["pack"].keys["run"])

    def test_attempts_match_the_first(self) -> None:
        """Every attempt uploads the same files the same way."""
        found = attempts()
        self.assertEqual(len(found), ATTEMPTS)
        for step in found[1:]:
            with self.subTest(step=step.id):
                self.assertEqual(step.keys["uses"], found[0].keys["uses"])
                self.assertEqual(step.maps["with"], found[0].maps["with"])

    def test_later_attempts_need_failure_and_overwrite(self) -> None:
        """Each later attempt runs only after a failure, with overwrite."""
        found = attempts()
        self.assertEqual(len(found), ATTEMPTS)
        for before, step in zip(found, found[1:], strict=False):
            with self.subTest(step=step.id):
                condition = step.keys["if"]
                self.assertIn(f"{OVERWRITE} == 'true'", condition)
                self.assertIn(f"steps.{before.id}.outcome == 'failure'", condition)

    def test_success_adds_no_wait(self) -> None:
        """An upload that succeeds at once runs alone, with no wait."""
        for overwrite in ("true", "false"):
            with self.subTest(overwrite=overwrite):
                run = simulate(overwrite, ["success"])
                self.assertEqual(run.ran, ["upload"])
                self.assertFalse(run.failed)
                self.assertEqual(run.slept, 0)

    def test_waits_lie_between_attempts(self) -> None:
        """A failed attempt is followed by a wait, then the next attempt."""
        for failures in range(1, ATTEMPTS):
            with self.subTest(failures=failures):
                run = simulate("true", ["failure"] * failures + ["success"])
                uploads = [label for label in run.ran if label.startswith("upload")]
                self.assertEqual(len(uploads), failures + 1)
                self.assertEqual(run.slept, sum(WAITS[:failures]))
                self.assertFalse(run.failed)
                for i, label in enumerate(run.ran):
                    if not label.startswith("upload"):
                        self.assertTrue(run.ran[i - 1].startswith("upload"))
                        self.assertTrue(run.ran[i + 1].startswith("upload"))

    def test_last_failure_fails_the_action(self) -> None:
        """When every attempt fails, the action fails and says so."""
        run = simulate("true", ["failure"] * ATTEMPTS)
        self.assertTrue(run.failed)
        self.assertEqual(run.slept, sum(WAITS))
        self.assertEqual(len(run.errors), 1)
        self.assertIn("${ARTIFACT_NAME}", run.errors[0])
        self.assertIn(f"{ATTEMPTS} attempts", run.errors[0])
        report = upload_section()[-1]
        self.assertIn('echo "::error::${message}"', report.keys["run"])
        self.assertEqual(
            report.maps["env"]["ARTIFACT_NAME"],
            "${{ steps.validate.outputs.artifact_name }}",
        )

    def test_no_overwrite_fails_at_once(self) -> None:
        """Without overwrite, a failed first upload fails the action."""
        run = simulate("false", ["failure"])
        self.assertEqual(run.ran, ["upload"])
        self.assertTrue(run.failed)
        self.assertEqual(run.slept, 0)

    def test_artifact_id_draws_on_every_attempt(self) -> None:
        """The artifact-id output reads the ID from every attempt."""
        value = artifact_id_value()
        found = attempts()
        self.assertEqual(len(found), ATTEMPTS)
        for step in found:
            with self.subTest(step=step.id):
                self.assertIn(f"steps.{step.id}.outputs.artifact-id", value)


if __name__ == "__main__":
    _ = unittest.main()
