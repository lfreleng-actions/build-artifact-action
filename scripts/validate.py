# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validate the inputs of build-artifact-action.

Reads the action inputs from INPUT_* environment variables, reports
every rule they break as a '::error::' line, and on success writes the
normalised values to $GITHUB_OUTPUT for the pack and unpack steps.
Nothing on disk is created or modified.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping

NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
ID_RE = re.compile(r"[0-9]+")
PATH_RE = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")

MODES = ("pack", "unpack")
BOOLEANS = ("true", "false")
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 90

# Each input: (environment variable, action default, the one mode it
# applies to or None for both). A mode-specific input given to the other
# mode is an error unless it still holds its default.
INPUTS: dict[str, tuple[str, str, str | None]] = {
    "mode": ("INPUT_MODE", "", None),
    "artifact-name": ("INPUT_ARTIFACT_NAME", "", None),
    "artifact-id": ("INPUT_ARTIFACT_ID", "", "unpack"),
    "path": ("INPUT_PATH", ".", "pack"),
    "paths": ("INPUT_PATHS", "", "pack"),
    "destination": ("INPUT_DESTINATION", "", "unpack"),
    "expected-digest": ("INPUT_EXPECTED_DIGEST", "", "unpack"),
    "retention-days": ("INPUT_RETENTION_DAYS", "1", "pack"),
    "fail-on-empty": ("INPUT_FAIL_ON_EMPTY", "true", "pack"),
    "overwrite": ("INPUT_OVERWRITE", "false", "pack"),
    "overlay": ("INPUT_OVERLAY", "false", "unpack"),
}

PATH_RULE = (
    "must be '.' or relative segments of [A-Za-z0-9._-] joined by '/',"
    " with no '..' segment"
)


CONTAINMENT_RULE = (
    "must resolve inside GITHUB_WORKSPACE with no symlink between the"
    " workspace and the target"
)

DIRECTORY_RULE = "must not be, or lie beneath, an existing path that is not a directory"


class ValidationError(Exception):
    """Raised with every rule the inputs break."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors: list[str] = errors


def path_grammar_ok(value: str) -> bool:
    """Return whether value is '.' or a relative, '..'-free path."""
    if value == ".":
        return True
    return PATH_RE.fullmatch(value) is not None and ".." not in value.split("/")


def inside(child: str, parent: str) -> bool:
    """Return whether the real path child is parent or below it."""
    return os.path.commonpath([child, parent]) == parent


def contained_root(workspace: str, value: str) -> str:
    """Resolve value under the canonical workspace, refusing symlinks.

    The workspace itself is the trusted anchor: the runner sets it, and
    its own path may legitimately pass through a symlink, so only the
    components from the workspace down to the target are checked.
    mkdir -p follows a symlinked ancestor, so each existing component is
    checked with lstat and the first symlink met is refused outright
    rather than resolved, before anything is created. An existing
    component that is not a directory is refused too, since nothing can
    be created beneath it. Raises ValueError naming the rule broken.
    """
    current = workspace
    for segment in value.split("/"):
        current = os.path.join(current, segment)
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        except OSError:
            raise ValueError(CONTAINMENT_RULE) from None
        if stat.S_ISLNK(mode):
            raise ValueError(CONTAINMENT_RULE)
        if not stat.S_ISDIR(mode):
            raise ValueError(DIRECTORY_RULE)
    resolved = os.path.realpath(os.path.join(workspace, value))
    if not inside(resolved, workspace):
        raise ValueError(CONTAINMENT_RULE)
    return resolved


def check_paths(raw: str, root: str | None, errors: list[str]) -> str:
    """Validate the newline-separated paths input; return it normalised."""
    entries: list[str] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        entry = line.strip()
        if not entry:
            continue
        if not path_grammar_ok(entry):
            errors.append(f"paths: line {number} {PATH_RULE}")
        elif root is not None and not inside(
            os.path.realpath(os.path.join(root, entry)), root
        ):
            errors.append(f"paths: line {number} must resolve inside 'path'")
        entries.append(entry)
    return "\n".join(entries)


def check_retention(value: str, errors: list[str]) -> str:
    """Validate retention-days; return it without leading zeros.

    The digit count is bounded before int() so an oversized value is
    reported, not raised past Python's integer-string conversion limit.
    """
    significant = value.lstrip("0")
    if (
        ID_RE.fullmatch(value) is None
        or len(significant) > len(str(MAX_RETENTION_DAYS))
        or not MIN_RETENTION_DAYS <= int(significant or "0") <= MAX_RETENTION_DAYS
    ):
        days = f"{MIN_RETENTION_DAYS} to {MAX_RETENTION_DAYS}"
        errors.append(f"retention-days: must be an integer from {days}")
        return value
    return significant


def check_location(
    name: str, value: str, workspace: str | None, errors: list[str]
) -> str | None:
    """Validate path or destination; return its absolute real path."""
    if not path_grammar_ok(value):
        errors.append(f"{name}: {PATH_RULE}")
        return None
    if workspace is None:
        return None
    try:
        return contained_root(workspace, value)
    except ValueError as error:
        errors.append(f"{name}: {error}")
        return None


def validate(env: Mapping[str, str]) -> dict[str, str]:
    """Return the normalised inputs, or raise ValidationError."""
    values = {
        name: env.get(variable, default)
        for name, (variable, default, _) in INPUTS.items()
    }
    errors: list[str] = []

    mode = values["mode"]
    if mode not in MODES:
        errors.append("mode: must be 'pack' or 'unpack'")
    else:
        for name, (_, default, only) in INPUTS.items():
            if only not in (None, mode) and values[name] != default:
                errors.append(f"{name}: applies only to '{only}' mode")

    if NAME_RE.fullmatch(values["artifact-name"]) is None:
        errors.append("artifact-name: must match ^[A-Za-z0-9._-]+$")

    workspace_env = env.get("GITHUB_WORKSPACE", "")
    workspace = os.path.realpath(workspace_env) if workspace_env else None
    if workspace is None:
        errors.append("GITHUB_WORKSPACE: must be set")

    out = {
        "mode": mode,
        "artifact_name": values["artifact-name"],
        "artifact_id": "",
        "root": "",
        "paths": "",
        "expected_digest": "",
        "retention_days": "",
        "fail_on_empty": "",
        "overwrite": "",
        "overlay": "",
    }

    if mode == "pack":
        root = check_location("path", values["path"], workspace, errors)
        out["root"] = root or ""
        out["paths"] = check_paths(values["paths"], root, errors)
        out["retention_days"] = check_retention(values["retention-days"], errors)
        for name in ("fail-on-empty", "overwrite"):
            if values[name] not in BOOLEANS:
                errors.append(f"{name}: must be exactly 'true' or 'false'")
            out[name.replace("-", "_")] = values[name]
    elif mode == "unpack":
        destination = values["destination"]
        if not destination:
            errors.append("destination: is required in 'unpack' mode")
        else:
            root = check_location("destination", destination, workspace, errors)
            out["root"] = root or ""
        artifact_id = values["artifact-id"]
        if artifact_id and ID_RE.fullmatch(artifact_id) is None:
            errors.append("artifact-id: must match ^[0-9]+$")
        out["artifact_id"] = artifact_id
        digest = values["expected-digest"]
        if digest and DIGEST_RE.fullmatch(digest) is None:
            errors.append(
                "expected-digest: must be a SHA-256 of 64 lower-case hex digits"
            )
        out["expected_digest"] = digest
        if values["overlay"] not in BOOLEANS:
            errors.append("overlay: must be exactly 'true' or 'false'")
        out["overlay"] = values["overlay"]

    if errors:
        raise ValidationError(errors)
    return out


def write_outputs(path: str, outputs: Mapping[str, str]) -> None:
    """Append outputs to path in the heredoc form, one random delimiter each.

    A delimiter absent from its value stops a value from injecting a
    second key into $GITHUB_OUTPUT.
    """
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            delimiter = f"ghadelim_{secrets.token_hex(16)}"
            while delimiter in value:
                delimiter = f"ghadelim_{secrets.token_hex(16)}"
            _ = handle.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def run(env: Mapping[str, str]) -> int:
    """Validate env and write the normalised values to GITHUB_OUTPUT."""
    try:
        outputs = validate(env)
    except ValidationError as error:
        for message in error.errors:
            print(f"::error::{message}")
        return 1
    output = env.get("GITHUB_OUTPUT", "")
    if not output:
        print("::error::GITHUB_OUTPUT: must be set")
        return 1
    write_outputs(output, outputs)
    print(f"Validated inputs for '{outputs['mode']}' mode ✅")
    return 0


if __name__ == "__main__":
    sys.exit(run(os.environ))
