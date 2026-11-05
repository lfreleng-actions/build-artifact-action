# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Check the README's Inputs and Outputs tables against action.yaml.

The README is the contract consuming workflows code against, so every
input and output must appear there with the name, required flag,
default, mode and grammar action.yaml gives it. The action uses only
the standard library, so this reads the small YAML subset action.yaml
uses rather than depending on a YAML package.
"""

from __future__ import annotations

import re
import unittest
from functools import cache
from pathlib import Path

from scripts.validate import PATH_RE

ROOT = Path(__file__).resolve().parent.parent

# Grammar fragments in an action.yaml description that the README row
# must repeat: an anchored regular expression, a numeric range, a hex
# length, or a boolean, which the README calls a strict boolean.
GRAMMAR = re.compile(r"\^\S+\$|\d+ to \d+|\d+ lower-case hex|true/false")

# The path inputs' descriptions carry no pattern GRAMMAR can see, so
# their rule is checked on its own: 'path' states it in full, using the
# segment class validate.py enforces, and the others point back to it.
SEGMENT = PATH_RE.pattern.partition("+")[0]
PATH_RULE = (
    f"`.` or relative segments of `{SEGMENT}`, with no `..` segment",
    "Must resolve inside `GITHUB_WORKSPACE`",
)
PATH_INHERITED = {
    "paths": "same grammar as `path`",
    "destination": "same grammar as `path`",
}


def unquote(value: str) -> str:
    """Strip one pair of matching quotes from a YAML scalar."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_action(text: str) -> dict[str, dict[str, dict[str, str]]]:
    """Return {'inputs': {...}, 'outputs': {...}} from action.yaml.

    Each entry maps a name to its scalar attributes. Folded scalars
    ('>-') are joined with single spaces, as YAML folds them.
    """
    sections: dict[str, dict[str, dict[str, str]]] = {
        "inputs": {},
        "outputs": {},
    }
    section: dict[str, dict[str, str]] | None = None
    entry: dict[str, str] | None = None
    key = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            section = sections.get(stripped.removesuffix(":"))
            entry = None
        elif section is None:
            continue
        elif indent == 2:
            entry = section.setdefault(stripped.removesuffix(":"), {})
            key = ""
        elif indent == 4 and entry is not None:
            key, _, value = stripped.partition(":")
            value = value.strip()
            entry[key] = "" if value in (">-", ">") else unquote(value)
        elif entry is not None and key:
            entry[key] = f"{entry[key]} {stripped}".strip()
    return sections


def parse_table(text: str, heading: str) -> dict[str, dict[str, str]]:
    """Return the first table under '## heading', keyed by its Name cell."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if match is None:
        raise AssertionError(f"README has no '## {heading}' section")
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in match.group(1).splitlines()
        if line.startswith("|")
    ]
    if len(rows) < 3:
        raise AssertionError(f"README '## {heading}' section has no table")
    header, body = rows[0], rows[2:]
    return {row[0]: dict(zip(header, row, strict=False)) for row in body}


def readme_default(cell: str) -> str | None:
    """Return the default a README cell states, None when it is blank."""
    if not cell:
        return None
    return unquote(cell.strip("`"))


def path_inputs(inputs: dict[str, dict[str, str]]) -> set[str]:
    """Return the inputs action.yaml describes as paths in the workspace."""
    return {
        name
        for name, spec in inputs.items()
        if "GITHUB_WORKSPACE" in spec["description"]
        or "under 'path'" in spec["description"]
    }


def path_rule_problems(table: dict[str, dict[str, str]]) -> list[str]:
    """Return how the Inputs table falls short of the path rule."""
    problems = [
        f"path: README does not state {rule!r}"
        for rule in PATH_RULE
        if rule not in table["path"]["Description"]
    ]
    problems.extend(
        f"{name}: README does not state {rule!r}"
        for name, rule in PATH_INHERITED.items()
        if rule not in table[name]["Description"].lower()
    )
    return problems


def action_mode(description: str) -> str:
    """Return the mode an action.yaml description is tagged with."""
    match = re.match(r"\[(pack|unpack)\]", description)
    return match.group(1) if match else "both"


@cache
def load_action() -> dict[str, dict[str, dict[str, str]]]:
    """Return the parsed action.yaml."""
    return parse_action((ROOT / "action.yaml").read_text(encoding="utf-8"))


@cache
def load_readme() -> str:
    """Return the text of README.md."""
    return (ROOT / "README.md").read_text(encoding="utf-8")


class ReadmeMatchesActionTest(unittest.TestCase):
    """The README tables document exactly what action.yaml declares."""

    @property
    def action(self) -> dict[str, dict[str, dict[str, str]]]:
        """Return the parsed action.yaml."""
        return load_action()

    @property
    def readme(self) -> str:
        """Return the text of README.md."""
        return load_readme()

    def test_parser_reads_action(self) -> None:
        """The parser finds the inputs and outputs action.yaml declares."""
        self.assertIn("mode", self.action["inputs"])
        self.assertEqual(self.action["inputs"]["path"]["default"], ".")
        self.assertIn("digest", self.action["outputs"])

    def test_input_names(self) -> None:
        """The Inputs table lists every input, and nothing else."""
        table = parse_table(self.readme, "Inputs")
        self.assertEqual(sorted(table), sorted(self.action["inputs"]))

    def test_input_rows(self) -> None:
        """Each Inputs row repeats the required flag, default and mode."""
        table = parse_table(self.readme, "Inputs")
        for name, spec in self.action["inputs"].items():
            with self.subTest(input=name):
                row = table[name]
                self.assertEqual(row["Required"].lower(), spec.get("required", "false"))
                self.assertEqual(readme_default(row["Default"]), spec.get("default"))
                self.assertEqual(row["Modes"], action_mode(spec["description"]))

    def test_input_grammar(self) -> None:
        """Each Inputs row states the grammar action.yaml gives."""
        table = parse_table(self.readme, "Inputs")
        for name, spec in self.action["inputs"].items():
            for match in GRAMMAR.finditer(spec["description"]):
                fragment = match.group(0)
                expected = "Strict boolean" if fragment == "true/false" else fragment
                with self.subTest(input=name, grammar=expected):
                    self.assertIn(expected, table[name]["Description"])

    def test_path_inputs_are_known(self) -> None:
        """Every path input in action.yaml has a path rule to check."""
        self.assertEqual(path_inputs(self.action["inputs"]), {"path", *PATH_INHERITED})

    def test_path_grammar(self) -> None:
        """The path inputs state the canonical path rule or inherit it."""
        self.assertEqual(SEGMENT, "[A-Za-z0-9._-]")
        self.assertEqual(path_rule_problems(parse_table(self.readme, "Inputs")), [])

    def test_path_grammar_drift_is_caught(self) -> None:
        """Dropping the path rule from any path row fails the check."""
        table = parse_table(self.readme, "Inputs")
        cases = [("path", rule) for rule in PATH_RULE]
        cases.extend(PATH_INHERITED.items())
        for name, rule in cases:
            with self.subTest(input=name, rule=rule):
                drifted = {key: dict(row) for key, row in table.items()}
                drifted[name]["Description"] = re.sub(
                    re.escape(rule), "", drifted[name]["Description"], flags=re.I
                )
                self.assertNotEqual(path_rule_problems(drifted), [])

    def test_outputs(self) -> None:
        """The Outputs table lists every output with its mode."""
        table = parse_table(self.readme, "Outputs")
        self.assertEqual(sorted(table), sorted(self.action["outputs"]))
        for name, spec in self.action["outputs"].items():
            with self.subTest(output=name):
                self.assertEqual(table[name]["Modes"], action_mode(spec["description"]))

    def test_drift_is_caught(self) -> None:
        """An input missing from the README fails the name check."""
        readme = self.readme.replace("| overwrite ", "| overwritten ")
        self.assertNotEqual(
            sorted(parse_table(readme, "Inputs")), sorted(self.action["inputs"])
        )


if __name__ == "__main__":
    _ = unittest.main()
