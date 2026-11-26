# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Check how action.yaml retries a failed overwriting upload.

GitHub's artefact service can fail to list an artefact for a moment
after its upload, so an overwriting upload that follows another can
miss the old artefact and collide with it. action.yaml repeats the
upload behind 'continue-on-error' when 'overwrite' is true. These tests
read the steps out of action.yaml with the standard library, as
tests/test_readme.py reads the inputs and outputs, and walk them the
way the runner would for a given sequence of upload outcomes.
"""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ATTEMPTS = 5
WAITS = [2, 4, 8, 16]
OVERWRITE = "steps.validate.outputs.overwrite"
UPLOAD = "actions/upload-artifact@"


@dataclass
class Step:
    """One entry of runs.steps: its scalar keys and its mappings."""

    keys: dict[str, str] = field(default_factory=dict)
    maps: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Return the step's id, or '' when it has none."""
        return self.keys.get("id", "")

    @property
    def uploads(self) -> bool:
        """Return whether the step is an actions/upload-artifact call."""
        return self.keys.get("uses", "").startswith(UPLOAD)


def unquote(value: str) -> str:
    """Strip one pair of matching quotes from a YAML scalar."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_steps(text: str) -> list[Step]:
    """Return runs.steps from action.yaml.

    Handles the subset action.yaml uses: '- ' opening a step at indent
    4, keys at indent 6, mapping entries ('with', 'env') at indent 8,
    and literal ('|') or folded ('>-') block scalars below either.
    """
    steps: list[Step] = []
    section = ""
    target: dict[str, str] = {}
    mapping: dict[str, str] = {}
    key = ""
    block = ""
    block_indent = 0
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if block and indent > block_indent:
            sep = "\n" if block == "|" else " "
            target[key] = f"{target[key]}{sep}{stripped}".lstrip()
            continue
        block = ""
        if stripped.startswith("#"):
            continue
        if indent <= 2:
            section = stripped if indent == 2 else ""
            continue
        if section != "steps:":
            continue
        if indent == 4 and stripped.startswith("- "):
            steps.append(Step())
            stripped, indent = stripped[2:], 6
        key, _, value = stripped.partition(":")
        value = value.strip()
        if indent == 6 and not value:
            mapping = steps[-1].maps.setdefault(key, {})
            continue
        target = steps[-1].keys if indent == 6 else mapping
        if value in ("|", ">-", ">"):
            block, block_indent = value, indent
            target[key] = ""
        else:
            target[key] = unquote(value)
    return steps


@cache
def action_text() -> str:
    """Return the text of action.yaml."""
    return (ROOT / "action.yaml").read_text(encoding="utf-8")


@cache
def load_steps() -> list[Step]:
    """Return the parsed runs.steps of action.yaml."""
    return parse_steps(action_text())


def artifact_id_value() -> str:
    """Return the expression the 'artifact-id' output takes its value from."""
    outputs = action_text().partition("\noutputs:\n")[2].partition("\nruns:")[0]
    match = re.search(r"^  artifact-id:\n(.*?)(?=^  \S)", outputs, re.M | re.S)
    if match is None:
        raise AssertionError("action.yaml declares no 'artifact-id' output")
    return match.group(1).partition("value:")[2]


CLAUSE = re.compile(r"^([\w.-]+) (==|!=) '([^']*)'$")


def evaluate(expression: str, context: dict[str, str]) -> bool:
    """Evaluate a conjunction of "name == 'value'" or "!=" clauses."""
    expression = expression.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    if expression in ("true", "false"):
        return expression == "true"
    result = True
    for clause in expression.split("&&"):
        match = CLAUSE.match(clause.strip())
        if match is None:
            raise AssertionError(f"cannot evaluate clause {clause.strip()!r}")
        name, operator, value = match.groups()
        actual = context.get(name, "")
        result = result and ((actual == value) == (operator == "=="))
    return result


@dataclass
class Run:
    """What the runner did with the upload steps in one simulated run."""

    ran: list[str]
    failed: bool
    slept: int
    errors: list[str]


def upload_section() -> list[Step]:
    """Return the steps from the first upload attempt to unpack's first."""
    steps = load_steps()
    first = next(i for i, step in enumerate(steps) if step.id == "upload")
    last = next(i for i, step in enumerate(steps) if step.id == "scratch")
    return steps[first:last]


def simulate(overwrite: str, outcomes: list[str]) -> Run:
    """Walk the upload steps with the given upload outcomes, in order.

    A step runs when no earlier step has failed the action and its 'if'
    holds. A failing step fails the action unless its
    'continue-on-error' holds; its outcome stays 'failure' either way.
    """
    context = {
        "inputs.mode": "pack",
        OVERWRITE: overwrite,
        "steps.validate.outputs.artifact_id": "",
    }
    pending = list(outcomes)
    run = Run(ran=[], failed=False, slept=0, errors=[])
    for step in upload_section():
        label = step.id or step.keys["name"]
        if run.failed or not evaluate(step.keys.get("if", "true"), context):
            if step.id:
                context[f"steps.{step.id}.outcome"] = "skipped"
            continue
        run.ran.append(label)
        script = step.keys.get("run", "")
        if step.uploads:
            outcome = pending.pop(0)
        elif "exit 1" in script:
            outcome = "failure"
            run.errors.extend(re.findall(r"^message=\"(.*)\"$", script, re.M))
        else:
            outcome = "success"
            sleep = re.search(r"^sleep (\d+)$", script, re.M)
            run.slept += int(sleep.group(1)) if sleep else 0
        if step.id:
            context[f"steps.{step.id}.outcome"] = outcome
        tolerated = evaluate(step.keys.get("continue-on-error", "false"), context)
        if outcome == "failure" and not tolerated:
            run.failed = True
    return run


def attempts() -> list[Step]:
    """Return the upload attempts in the order they appear."""
    return [step for step in upload_section() if step.uploads]


class UploadRetryTest(unittest.TestCase):
    """An overwriting upload is retried; any other upload is not."""

    def test_parser_reads_steps(self) -> None:
        """The parser finds the steps and their 'with' entries."""
        steps = {step.id: step for step in load_steps() if step.id}
        self.assertIn("validate", steps)
        self.assertEqual(steps["upload"].maps["with"]["compression-level"], "0")
        self.assertIn("pack.py", steps["pack"].keys["run"])

    @unittest.expectedFailure
    def test_attempts_match_the_first(self) -> None:
        """Every attempt uploads the same files the same way."""
        found = attempts()
        self.assertEqual(len(found), ATTEMPTS)
        for step in found[1:]:
            with self.subTest(step=step.id):
                self.assertEqual(step.keys["uses"], found[0].keys["uses"])
                self.assertEqual(step.maps["with"], found[0].maps["with"])

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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
