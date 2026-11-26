# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Check how action.yaml retries a failed artefact download.

GitHub's artefact service can fail to list an artefact for a moment
after its upload, so a download straight after one can find nothing,
by name or by ID. action.yaml repeats each download behind
'continue-on-error' and empties the scratch directory before each
retry. These tests read the steps out of action.yaml with
tests/action_steps.py and walk them the way the runner would for a
given sequence of download outcomes.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from tests.action_steps import Run, Step, section, walk

ATTEMPTS = 5
WAITS = [2, 4, 8, 16]
DOWNLOAD = "actions/download-artifact@"
ARTIFACT_ID = "steps.validate.outputs.artifact_id"
SCRATCH = "${{ steps.scratch.outputs.path }}"

# Each download path: the 'with' key that selects the artefact, its
# value, the 'artifact-id' that selects the path, and the clause on it.
PATHS = {
    "name": (
        "name",
        "${{ steps.validate.outputs.artifact_name }}",
        "",
        f"{ARTIFACT_ID} == ''",
    ),
    "id": (
        "artifact-ids",
        f"${{{{ {ARTIFACT_ID} }}}}",
        "42",
        f"{ARTIFACT_ID} != ''",
    ),
}


def download_section() -> list[Step]:
    """Return the steps from the scratch directory to the end."""
    return section("scratch")


def attempts(by: str) -> list[Step]:
    """Return the download attempts of one path in the order they appear."""
    key = PATHS[by][0]
    return [
        step
        for step in download_section()
        if step.calls(DOWNLOAD) and key in step.maps.get("with", {})
    ]


def simulate(
    by: str,
    outcomes: list[str],
    visit: Callable[[Step, str], None] | None = None,
) -> Run:
    """Walk the download steps with the given download outcomes, in order."""
    context = {"inputs.mode": "unpack", ARTIFACT_ID: PATHS[by][2]}
    return walk(download_section(), context, DOWNLOAD, outcomes, visit)


def downloads(run: Run) -> list[str]:
    """Return the labels of the download attempts that ran."""
    labels = {step.label for step in download_section() if step.calls(DOWNLOAD)}
    return [label for label in run.ran if label in labels]


def tree(root: Path) -> list[str]:
    """Return every path under root, relative to it, sorted."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


class Scratch:
    """A real scratch directory that simulated steps fill and empty.

    Each download attempt, failed or not, leaves files in it. A 'run'
    step that reads DOWNLOAD_DIR, other than Unpack, runs in bash
    against it with 'sleep' stubbed out. Unpack records what it holds.
    """

    def __init__(self, root: Path) -> None:
        """Use root as the scratch directory."""
        self.root: Path = root
        self.seen: list[list[str]] = []

    def visit(self, step: Step, outcome: str) -> None:
        """Act out one step that ran, with its outcome."""
        if step.calls(DOWNLOAD):
            _ = (self.root / f"{step.label}.tar").write_text(outcome)
            _ = (self.root / f".{step.label}").write_text(outcome)
            (self.root / step.label / "nested").mkdir(parents=True)
        elif step.id == "unpack":
            self.seen.append(tree(self.root))
        elif "DOWNLOAD_DIR" in step.maps.get("env", {}):
            script = "sleep() { :; }\n" + step.keys["run"]
            _ = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                env={"PATH": os.environ["PATH"], "DOWNLOAD_DIR": str(self.root)},
                check=True,
            )


class DownloadRetryTest(unittest.TestCase):
    """A failed download is retried, by name and by ID alike."""

    def test_attempts_match_the_first(self) -> None:
        """Every attempt downloads the same artefact into the scratch."""
        for by, (key, value, _, _) in PATHS.items():
            found = attempts(by)
            with self.subTest(by=by):
                self.assertEqual(len(found), ATTEMPTS)
                self.assertEqual(found[0].maps["with"], {key: value, "path": SCRATCH})
                for step in found:
                    self.assertEqual(step.keys["uses"], found[0].keys["uses"])
                    self.assertEqual(step.maps["with"], found[0].maps["with"])
                    self.assertEqual(step.keys.get("continue-on-error"), "true")

    def test_later_attempts_need_failure(self) -> None:
        """Each later attempt runs only after a failure, on its own path."""
        for by, (_, _, _, clause) in PATHS.items():
            found = attempts(by)
            with self.subTest(by=by):
                self.assertEqual(len(found), ATTEMPTS)
                self.assertIn(clause, found[0].keys["if"])
                for before, step in zip(found, found[1:], strict=False):
                    condition = step.keys["if"]
                    self.assertIn(clause, condition)
                    self.assertIn(f"steps.{before.id}.outcome == 'failure'", condition)

    def test_success_adds_no_wait(self) -> None:
        """A download that succeeds at once runs alone, then unpack."""
        for by in PATHS:
            with self.subTest(by=by):
                run = simulate(by, ["success"])
                self.assertEqual(run.ran, ["scratch", attempts(by)[0].label, "unpack"])
                self.assertFalse(run.failed)
                self.assertEqual(run.slept, 0)

    def test_waits_lie_between_attempts(self) -> None:
        """A failed attempt is followed by a wait, then the next attempt."""
        for by in PATHS:
            for failures in range(1, ATTEMPTS):
                with self.subTest(by=by, failures=failures):
                    run = simulate(by, ["failure"] * failures + ["success"])
                    ran = downloads(run)
                    self.assertEqual(len(ran), failures + 1)
                    self.assertEqual(run.slept, sum(WAITS[:failures]))
                    self.assertFalse(run.failed)
                    self.assertEqual(run.ran[0], "scratch")
                    self.assertEqual(run.ran[-1], "unpack")
                    middle = run.ran[1:-1]
                    self.assertEqual([middle[0], middle[-1]], [ran[0], ran[-1]])
                    for i, label in enumerate(middle):
                        if label not in ran:
                            self.assertIn(middle[i - 1], ran)
                            self.assertIn(middle[i + 1], ran)

    def test_last_failure_fails_the_action(self) -> None:
        """When every attempt fails, the action fails before unpack."""
        steps = {step.label: step for step in download_section()}
        for by in PATHS:
            with self.subTest(by=by):
                run = simulate(by, ["failure"] * ATTEMPTS)
                self.assertTrue(run.failed)
                self.assertNotIn("unpack", run.ran)
                self.assertEqual(len(downloads(run)), ATTEMPTS)
                self.assertEqual(run.slept, sum(WAITS))
                self.assertEqual(len(run.errors), 1)
                self.assertIn("${artefact}", run.errors[0])
                self.assertIn(f"{ATTEMPTS} attempts", run.errors[0])
                report = steps[run.ran[-1]]
                script = report.keys["run"]
                self.assertIn('echo "::error::${message}"', script)
                named: list[str] = re.findall(r'^artefact="(.*)"$', script, re.M)
                self.assertEqual(len(named), 1)
                self.assertIn("'${ARTIFACT_NAME}'", named[0])
                env = report.maps["env"]
                self.assertEqual(env["ARTIFACT_NAME"], PATHS["name"][1])
                if by == "id":
                    self.assertIn("${ARTIFACT_ID}", named[0])
                    self.assertEqual(env["ARTIFACT_ID"], PATHS["id"][1])

    def test_paths_stay_apart(self) -> None:
        """No attempt of the other path runs, whatever the outcomes."""
        for by, other in (("name", "id"), ("id", "name")):
            excluded = {step.label for step in attempts(other)}
            for failures in range(ATTEMPTS + 1):
                outcomes = ["failure"] * failures + ["success"]
                with self.subTest(by=by, failures=failures):
                    run = simulate(by, outcomes[:ATTEMPTS])
                    self.assertFalse(excluded & set(run.ran))

    def test_unpack_reads_one_attempt(self) -> None:
        """Unpack finds only the files of the attempt that succeeded."""
        for step in download_section():
            env = step.maps.get("env", {})
            if "DOWNLOAD_DIR" in env:
                with self.subTest(step=step.label):
                    self.assertEqual(env["DOWNLOAD_DIR"], SCRATCH)
        for by in PATHS:
            for failures in range(ATTEMPTS):
                with (
                    self.subTest(by=by, failures=failures),
                    tempfile.TemporaryDirectory() as root,
                ):
                    scratch = Scratch(Path(root))
                    outcomes = ["failure"] * failures + ["success"]
                    run = simulate(by, outcomes, scratch.visit)
                    self.assertFalse(run.failed)
                    last = downloads(run)[-1]
                    expected = [f".{last}", last, f"{last}.tar", f"{last}/nested"]
                    self.assertEqual(scratch.seen, [sorted(expected)])


if __name__ == "__main__":
    _ = unittest.main()
