# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Read the steps out of action.yaml and walk them as the runner would.

The retry tests for the upload and the download share this module. It
reads the small YAML subset action.yaml uses with the standard library,
as tests/test_readme.py reads the inputs and outputs, and walks a range
of steps for a given sequence of outcomes of the steps that call an
action.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
    def label(self) -> str:
        """Return the step's id, or its name when it has no id."""
        return self.id or self.keys["name"]

    def calls(self, prefix: str) -> bool:
        """Return whether the step uses an action starting with prefix."""
        return self.keys.get("uses", "").startswith(prefix)


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


def section(first: str, end: str | None = None) -> list[Step]:
    """Return the steps from the one with id first up to id end.

    The step with id end is excluded; with no end, the range runs to
    the last step.
    """
    steps = load_steps()
    ids = [step.id for step in steps]
    start = ids.index(first)
    stop = ids.index(end) if end is not None else len(steps)
    return steps[start:stop]


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
    """What the runner did with a range of steps in one simulated run."""

    ran: list[str]
    failed: bool
    slept: int
    errors: list[str]


def walk(
    steps: list[Step],
    context: dict[str, str],
    prefix: str,
    outcomes: list[str],
    visit: Callable[[Step, str], None] | None = None,
) -> Run:
    """Walk steps, giving the calls to prefix the outcomes, in order.

    A step runs when no earlier step has failed the action and its 'if'
    holds. A step that calls an action starting with prefix takes the
    next of outcomes; a 'run' step that exits 1 fails, and any other
    succeeds. A failing step fails the action unless its
    'continue-on-error' holds; its outcome stays 'failure' either way.
    visit, when given, sees each step that ran and its outcome.
    """
    context = dict(context)
    pending = list(outcomes)
    run = Run(ran=[], failed=False, slept=0, errors=[])
    for step in steps:
        if run.failed or not evaluate(step.keys.get("if", "true"), context):
            if step.id:
                context[f"steps.{step.id}.outcome"] = "skipped"
            continue
        run.ran.append(step.label)
        script = step.keys.get("run", "")
        if step.calls(prefix):
            outcome = pending.pop(0)
        elif "exit 1" in script:
            outcome = "failure"
            run.errors.extend(re.findall(r"^message=\"(.*)\"$", script, re.M))
        else:
            outcome = "success"
            sleep = re.search(r"^sleep (\d+)$", script, re.M)
            run.slept += int(sleep.group(1)) if sleep else 0
        if visit is not None:
            visit(step, outcome)
        if step.id:
            context[f"steps.{step.id}.outcome"] = outcome
        tolerated = evaluate(step.keys.get("continue-on-error", "false"), context)
        if outcome == "failure" and not tolerated:
            run.failed = True
    return run
