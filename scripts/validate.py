# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validate the inputs of build-artifact-action (not yet implemented)."""

from __future__ import annotations

from collections.abc import Mapping


class ValidationError(Exception):
    """Raised with every rule the inputs break."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors: list[str] = errors


def validate(env: Mapping[str, str]) -> dict[str, str]:
    """Return the normalised inputs, or raise ValidationError."""
    _ = env
    raise NotImplementedError


def run(env: Mapping[str, str]) -> int:
    """Validate env and write the normalised values to GITHUB_OUTPUT."""
    _ = env
    raise NotImplementedError
