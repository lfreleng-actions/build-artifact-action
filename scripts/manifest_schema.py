# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The schema of the build-artifact-action digest manifest.

Defines the manifest's types and the checks load() applies, so that a
malformed manifest fails on its first problem with one '::error::'
line and a ManifestError carrying the same message.
"""

from __future__ import annotations

import re
from typing import NoReturn, TypedDict, cast

SCHEMA = 1
ACTION = "lfreleng-actions/build-artifact-action"

SHA256_RE = re.compile(r"[0-9a-f]{64}")
MODE_RE = re.compile(r"[0-7]{4}")
TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

# Manifest keys filled from the runner's environment.
RUN_KEYS = {
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": "GITHUB_RUN_ATTEMPT",
    "repository": "GITHUB_REPOSITORY",
    "sha": "GITHUB_SHA",
}
STRING_KEYS = ("action", "artifact_name", "root", "created_at", *RUN_KEYS)
MANIFEST_KEYS = frozenset(("schema", "tar", "files", *STRING_KEYS))
TAR_KEYS = frozenset(("path", "sha256", "size"))
REGULAR_KEYS = frozenset(("path", "sha256", "size", "mode"))
SYMLINK_KEYS = frozenset(("path", "symlink"))


class TarEntry(TypedDict):
    """The tar beside the manifest: its file name, SHA-256 and size."""

    path: str
    sha256: str
    size: int


class RegularEntry(TypedDict):
    """A regular file: its SHA-256, size and octal permission bits."""

    path: str
    sha256: str
    size: int
    mode: str


class SymlinkEntry(TypedDict):
    """A symlink: its target as stored, never resolved."""

    path: str
    symlink: str


FileEntry = RegularEntry | SymlinkEntry


class Manifest(TypedDict):
    """A parsed manifest, its paths relative to 'root'."""

    schema: int
    action: str
    artifact_name: str
    root: str
    created_at: str
    run_id: str
    run_attempt: str
    repository: str
    sha: str
    tar: TarEntry
    files: list[FileEntry]


class ManifestError(Exception):
    """Raised with the first problem found in a manifest or tree."""


def escape_annotation(message: str) -> str:
    """Escape '%', CR and LF so message stays one workflow command.

    A lone surrogate, which no UTF-8 stream can carry, is printed as a
    backslash escape rather than failing the print.
    """
    text = message.encode("utf-8", "backslashreplace").decode("utf-8")
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def fail(message: str) -> NoReturn:
    """Print message as one '::error::' line and raise ManifestError.

    The printed copy is escaped, so a path holding a newline cannot
    start a second workflow command; the exception keeps the original.
    """
    print(f"::error::{escape_annotation(message)}")
    raise ManifestError(message)


def utf8_ok(text: str) -> bool:
    """Return whether text encodes as UTF-8 (no lone surrogates)."""
    try:
        _ = text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def path_problem(path: str) -> str | None:
    """Return why path is not a safe relative POSIX path, or None."""
    if not path:
        return "is empty"
    if not utf8_ok(path):
        return "is not valid UTF-8"
    if path.startswith("/"):
        return "is absolute"
    if "\0" in path:
        return "contains a NUL byte"
    segments = path.split("/")
    if ".." in segments:
        return "contains a '..' segment"
    if "." in segments or "" in segments:
        return "is not normalised"
    return None


def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, refusing a key that appears twice."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def refuse_constant(name: str) -> NoReturn:
    """Refuse NaN and Infinity, which are not JSON."""
    raise ValueError(f"invalid constant {name}")


def is_int(value: object) -> bool:
    """Return whether value is an int and not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def check_keys(where: str, value: object, keys: frozenset[str]) -> dict[str, object]:
    """Return value as an object holding exactly keys, or fail."""
    if not isinstance(value, dict):
        fail(f"{where}: must be an object")
    obj = cast("dict[str, object]", value)
    missing = sorted(keys - obj.keys())
    if missing:
        fail(f"{where}: missing key {missing[0]!r}")
    extra = sorted(obj.keys() - keys)
    if extra:
        fail(f"{where}: unexpected key {extra[0]!r}")
    return obj


def check_digest(where: str, obj: dict[str, object]) -> None:
    """Fail unless obj holds a SHA-256 and a non-negative size."""
    sha256 = obj["sha256"]
    if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
        fail(f"{where}.sha256: must be 64 lower-case hex digits")
    size = obj["size"]
    if not is_int(size) or cast("int", size) < 0:
        fail(f"{where}.size: must be a non-negative integer")


def has_key(value: object, key: str) -> bool:
    """Return whether value is an object holding key."""
    return isinstance(value, dict) and key in cast("dict[str, object]", value)


def check_file(index: int, value: object) -> str:
    """Fail unless value is a valid files entry; return its path."""
    where = f"files[{index}]"
    symlink = has_key(value, "symlink")
    obj = check_keys(where, value, SYMLINK_KEYS if symlink else REGULAR_KEYS)
    if symlink:
        target = obj["symlink"]
        if not isinstance(target, str) or not target or not utf8_ok(target):
            fail(f"{where}.symlink: must be a non-empty UTF-8 string")
    else:
        check_digest(where, obj)
        mode = obj["mode"]
        if not isinstance(mode, str) or MODE_RE.fullmatch(mode) is None:
            fail(f"{where}.mode: must be four octal digits, 0000 to 7777")
    path = obj["path"]
    if not isinstance(path, str):
        fail(f"{where}.path: must be a string")
    problem = path_problem(path)
    if problem is not None:
        fail(f"{where}.path: {path!r} {problem}")
    return path


def check_files(value: object) -> None:
    """Fail unless value is a sorted list of valid, non-nested entries.

    No path may sit below another listed path: the earlier one is a
    file or a symlink, so the later one could not be extracted, or
    would be extracted through the link.
    """
    if not isinstance(value, list):
        fail("files: must be a list")
    seen: set[str] = set()
    previous: str | None = None
    for index, item in enumerate(cast("list[object]", value)):
        path = check_file(index, item)
        if previous is not None and path <= previous:
            fail(f"files[{index}].path: {path!r} is not sorted after {previous!r}")
        segments = path.split("/")
        for depth in range(1, len(segments)):
            parent = "/".join(segments[:depth])
            if parent in seen:
                fail(f"files[{index}].path: {path!r} lies below listed {parent!r}")
        seen.add(path)
        previous = path


def validate(value: object) -> Manifest:
    """Return value as a Manifest, or fail naming the first problem."""
    obj = check_keys("manifest", value, MANIFEST_KEYS)
    schema = obj["schema"]
    if not is_int(schema) or schema != SCHEMA:
        fail(f"schema: must be {SCHEMA}")
    for key in STRING_KEYS:
        text = obj[key]
        if not isinstance(text, str) or not utf8_ok(text):
            fail(f"{key}: must be a UTF-8 string")
    root = cast("str", obj["root"])
    problem = path_problem(root)
    if problem is not None:
        fail(f"root: {root!r} {problem}")
    created_at = cast("str", obj["created_at"])
    if TIMESTAMP_RE.fullmatch(created_at) is None:
        fail("created_at: must be a UTC timestamp like 2026-01-31T12:00:00Z")
    tar = check_keys("tar", obj["tar"], TAR_KEYS)
    tar_path = tar["path"]
    if not isinstance(tar_path, str) or path_problem(tar_path) or "/" in tar_path:
        fail("tar.path: must be a file name with no directory")
    check_digest("tar", tar)
    check_files(obj["files"])
    return cast("Manifest", cast("object", obj))
