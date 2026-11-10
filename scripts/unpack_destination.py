# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The destination directory 'unpack' mode creates and, on failure, removes."""

from __future__ import annotations

import os
import posixpath
import shutil
import stat

from scripts.manifest_schema import fail
from scripts.unpack_modes import open_up
from scripts.validate import PATH_RULE, inside, path_grammar_ok


def plan_destination(workspace: str, value: str) -> tuple[str, list[str]]:
    """Return the absolute destination and its missing ancestors, in order.

    workspace is already canonical. Every existing component below it
    must be a real directory, never a symlink, and the destination
    itself must not exist: only a newly created root guarantees that no
    pre-existing link beneath it can redirect a write.
    """
    if not path_grammar_ok(value):
        fail(f"destination: {PATH_RULE}")
    # The grammar admits '.' segments, which would otherwise survive into
    # the paths created and checked below.
    value = posixpath.normpath(value)
    if value == ".":
        fail("destination: must not exist; '.' is the workspace itself")
    segments = value.split("/")
    current = workspace
    missing: list[str] = []
    for depth, segment in enumerate(segments[:-1], start=1):
        current = os.path.join(current, segment)
        if missing:
            missing.append(current)
            continue
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            missing.append(current)
            continue
        except OSError as error:
            fail(f"destination: cannot stat {current}: {error.strerror}")
        ancestor = "/".join(segments[:depth])
        if stat.S_ISLNK(mode):
            fail(f"destination: ancestor {ancestor!r} is a symlink")
        if not stat.S_ISDIR(mode):
            fail(f"destination: ancestor {ancestor!r} is not a directory")
    final = os.path.join(current, segments[-1])
    if not missing and os.path.lexists(final):
        fail(f"destination: {value!r} must not exist; unpack creates it")
    return final, missing


def create_destination(
    workspace: str, final: str, missing: list[str], created: list[str]
) -> None:
    """Create each missing ancestor, then final, recording each in created."""
    for path in [*missing, final]:
        try:
            os.mkdir(path)
        except FileExistsError:
            fail(f"destination: {path} must not exist; unpack creates it")
        except OSError as error:
            fail(f"destination: cannot create {path}: {error.strerror}")
        created.append(path)
    if os.path.realpath(final) != final or not inside(final, workspace):
        fail(f"destination: {final} does not resolve inside the workspace")


def remove_created(created: list[str]) -> list[str]:
    """Remove what create_destination made: the tree, then empty parents.

    Returns an error message for each path left behind.
    """
    problems: list[str] = []
    for path in reversed(created):
        try:
            if path == created[-1]:
                open_up(path)
                shutil.rmtree(path)
            else:
                os.rmdir(path)
        except OSError as error:
            problems.append(f"cannot remove {path}: {error.strerror}")
    return problems
