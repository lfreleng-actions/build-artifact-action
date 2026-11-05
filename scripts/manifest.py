# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Write and verify the digest manifest (not implemented yet)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypedDict


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


def build(
    root: str,
    entries: Iterable[str],
    *,
    artifact_name: str,
    tar_path: str,
    root_name: str | None = None,
    env: Mapping[str, str] | None = None,
    created_at: str | None = None,
) -> Manifest:
    """Return the manifest for entries under root and the tar at tar_path."""
    _ = (root, entries, artifact_name, tar_path, root_name, env, created_at)
    raise NotImplementedError


def write(manifest: Manifest, path: str) -> str:
    """Write manifest to path in canonical form; return its SHA-256."""
    _ = (manifest, path)
    raise NotImplementedError


def load(path: str) -> Manifest:
    """Parse and validate the manifest at path."""
    _ = path
    raise NotImplementedError


def verify_tar(manifest: Manifest, tar_path: str) -> None:
    """Fail unless the tar at tar_path has the size and SHA-256 recorded."""
    _ = (manifest, tar_path)
    raise NotImplementedError


def verify_tree(manifest: Manifest, root: str) -> int:
    """Fail unless the tree under root is exactly what manifest lists."""
    _ = (manifest, root)
    raise NotImplementedError
