# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/manifest.py."""

from __future__ import annotations

import copy
import errno
import hashlib
import io
import json
import os
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, redirect_stdout
from functools import partial
from pathlib import Path
from typing import cast
from unittest import mock

import scripts.manifest as manifest_module
from scripts.manifest import (
    Manifest,
    ManifestError,
    build,
    load,
    verify_tar,
    verify_tree,
    write,
)

ENV = {
    "GITHUB_RUN_ID": "41830119310",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_REPOSITORY": "onap/cps",
    "GITHUB_SHA": "32250c531" + "0" * 31,
}
CREATED_AT = "2026-11-04T12:00:00Z"


class ManifestTestCase(unittest.TestCase):
    """Build manifests over a throwaway tree and tar."""

    _base: Path | None = None

    @property
    def base(self) -> Path:
        """Return a temporary directory for this test."""
        if self._base is None:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            self._base = Path(os.path.realpath(tmp.name))
        return self._base

    @property
    def root(self) -> Path:
        """Return the tree the manifest describes, creating it once."""
        root = self.base / "m2repo"
        if not root.exists():
            (root / "bin").mkdir(parents=True)
            (root / "org" / "pkg").mkdir(parents=True)
            (root / "empty").mkdir()
            _ = (root / "org" / "pkg" / "a.pom").write_text("<project/>\n")
            _ = (root / "bin" / "run.sh").write_text("#!/bin/sh\necho run\n")
            (root / "bin" / "run.sh").chmod(0o755)
            (root / "org" / "pkg" / "a.pom").chmod(0o644)
            (root / "link").symlink_to("bin/run.sh")
        return root

    @property
    def tar(self) -> Path:
        """Return a stand-in for the packed tar, creating it once."""
        tar = self.base / "build-artifact.tar"
        if not tar.exists():
            _ = tar.write_bytes(b"tar bytes\n" * 100)
        return tar

    def build(self, entries: list[str] | None = None) -> Manifest:
        """Build the manifest for entries (default: the whole root)."""
        return build(
            str(self.root),
            ["."] if entries is None else entries,
            artifact_name="maven-m2repo",
            tar_path=str(self.tar),
            env=ENV,
            created_at=CREATED_AT,
        )

    def invalid_roots(self) -> list[str]:
        """Return a missing root, a regular file and a symlinked directory.

        The symlink is also spelled with a trailing separator and a
        trailing '.', which lstat would otherwise follow.
        """
        missing = self.base / "absent"
        plain = self.base / "plain"
        _ = plain.write_text("not a directory\n")
        linked = self.base / "linked"
        linked.symlink_to(self.root)
        return [str(missing), str(plain), str(linked), f"{linked}/", f"{linked}/."]

    def fifo(self) -> str:
        """Return the path of a FIFO no process writes to."""
        fifo = self.base / "fifo.tar"
        os.mkfifo(fifo)
        return str(fifo)

    def unreadable_dir(self) -> None:
        """Make root/empty unlistable; skip when running as root."""
        if os.geteuid() == 0:
            self.skipTest("root can list any directory")
        empty = self.root / "empty"
        empty.chmod(0)
        self.addCleanup(empty.chmod, 0o755)

    def read_error(self) -> AbstractContextManager[object]:
        """Return a patch making fstat of an open file fail with EIO."""
        error = OSError(errno.EIO, os.strerror(errno.EIO))
        return mock.patch("scripts.manifest.os.fstat", side_effect=error)

    def assert_fails(self, needle: str, call: Callable[[], object]) -> None:
        """Assert call raises ManifestError naming needle in one '::error::'."""
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(ManifestError) as caught:
            _ = call()
        message = str(caught.exception)
        self.assertIn(needle, message)
        self.assertEqual(out.getvalue(), f"::error::{message}\n")


class BuildTest(ManifestTestCase):
    """build() and write() produce the documented, canonical format."""

    def test_structure(self) -> None:
        manifest = self.build()
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["action"], "lfreleng-actions/build-artifact-action")
        self.assertEqual(manifest["artifact_name"], "maven-m2repo")
        self.assertEqual(manifest["root"], "m2repo")
        self.assertEqual(manifest["created_at"], CREATED_AT)
        self.assertEqual(manifest["run_id"], ENV["GITHUB_RUN_ID"])
        self.assertEqual(manifest["run_attempt"], ENV["GITHUB_RUN_ATTEMPT"])
        self.assertEqual(manifest["repository"], ENV["GITHUB_REPOSITORY"])
        self.assertEqual(manifest["sha"], ENV["GITHUB_SHA"])
        tar = self.tar.read_bytes()
        self.assertEqual(
            manifest["tar"],
            {
                "path": "build-artifact.tar",
                "sha256": hashlib.sha256(tar).hexdigest(),
                "size": len(tar),
            },
        )
        pom = (self.root / "org" / "pkg" / "a.pom").read_bytes()
        run = (self.root / "bin" / "run.sh").read_bytes()
        self.assertEqual(
            manifest["files"],
            [
                {
                    "path": "bin/run.sh",
                    "sha256": hashlib.sha256(run).hexdigest(),
                    "size": len(run),
                    "mode": "0755",
                },
                {"path": "link", "symlink": "bin/run.sh"},
                {
                    "path": "org/pkg/a.pom",
                    "sha256": hashlib.sha256(pom).hexdigest(),
                    "size": len(pom),
                    "mode": "0644",
                },
            ],
        )

    def test_round_trip(self) -> None:
        manifest = self.build()
        first = self.base / "first.json"
        second = self.base / "second.json"
        digest = write(manifest, str(first))
        self.assertEqual(load(str(first)), manifest)
        self.assertEqual(write(manifest, str(second)), digest)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digest)

    def test_canonical_json(self) -> None:
        path = self.base / "manifest.json"
        manifest = self.build()
        _ = write(manifest, str(path))
        text = path.read_text(encoding="utf-8")
        expected = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
        self.assertEqual(text, expected + "\n")

    def test_entry_order_does_not_matter(self) -> None:
        orders = (
            ["."],
            ["org", "link", "bin"],
            ["link", "bin/run.sh", "org/pkg/a.pom", "bin"],
        )
        digests = {
            write(self.build(order), str(self.base / f"m{index}.json"))
            for index, order in enumerate(orders)
        }
        self.assertEqual(len(digests), 1)

    def test_symlink_target_not_hashed(self) -> None:
        (self.root / "dangling").symlink_to("no/such/file")
        (self.root / "dirlink").symlink_to("org")
        files = self.build()["files"]
        self.assertIn({"path": "dangling", "symlink": "no/such/file"}, files)
        self.assertIn({"path": "dirlink", "symlink": "org"}, files)
        self.assertFalse(any(f["path"].startswith("dirlink/") for f in files))

    def test_subset_of_entries(self) -> None:
        paths = [f["path"] for f in self.build(["org"])["files"]]
        self.assertEqual(paths, ["org/pkg/a.pom"])

    def test_missing_entry_rejected(self) -> None:
        self.assert_fails("absent", lambda: self.build(["absent"]))

    def test_unsafe_entry_rejected(self) -> None:
        for entry in ("../m2repo", "/etc", "bin/../org"):
            with self.subTest(entry=entry):
                self.assert_fails(entry, partial(self.build, [entry]))

    def test_entry_through_symlink_rejected(self) -> None:
        (self.root / "dirlink").symlink_to("org")
        self.assert_fails("dirlink", partial(self.build, ["dirlink/pkg/a.pom"]))

    def test_special_file_rejected(self) -> None:
        os.mkfifo(self.root / "pipe")
        self.assert_fails("pipe", self.build)

    def test_invalid_root_rejected(self) -> None:
        for root in self.invalid_roots():
            with self.subTest(root=root):
                call = partial(
                    build,
                    root,
                    ["."],
                    artifact_name="maven-m2repo",
                    tar_path=str(self.tar),
                    env=ENV,
                    created_at=CREATED_AT,
                )
                self.assert_fails(root, call)

    def test_root_name_from_traversed_spelling(self) -> None:
        elsewhere = self.base / "x" / "y"
        elsewhere.mkdir(parents=True)
        (self.root / "jump").symlink_to(elsewhere)
        manifest = build(
            f"{self.root}/jump/..",
            ["."],
            artifact_name="maven-m2repo",
            tar_path=str(self.tar),
            env=ENV,
            created_at=CREATED_AT,
        )
        self.assertEqual(manifest["root"], "m2repo")
        self.assertIn({"path": "jump", "symlink": str(elsewhere)}, manifest["files"])

    def test_unsafe_root_name_rejected(self) -> None:
        for root_name in ("", "/tmp/out", "../outside"):
            with self.subTest(root_name=root_name):
                call = partial(
                    build,
                    str(self.root),
                    ["."],
                    artifact_name="maven-m2repo",
                    tar_path=str(self.tar),
                    root_name=root_name,
                    env=ENV,
                    created_at=CREATED_AT,
                )
                self.assert_fails("root name", call)

    def test_fifo_tar_rejected(self) -> None:
        call = partial(
            build,
            str(self.root),
            ["."],
            artifact_name="maven-m2repo",
            tar_path=self.fifo(),
            env=ENV,
            created_at=CREATED_AT,
        )
        self.assert_fails("not a regular file", call)

    def test_read_error_rejected(self) -> None:
        _ = (self.root, self.tar)
        with self.read_error():
            self.assert_fails("cannot read", self.build)

    def test_unreadable_dir_rejected(self) -> None:
        self.unreadable_dir()
        self.assert_fails("empty: cannot list directory", self.build)

    def test_write_failure_rejected(self) -> None:
        manifest = self.build()
        for path in (self.base / "absent" / "manifest.json", self.base):
            with self.subTest(path=path.name):
                self.assert_fails("cannot write", partial(write, manifest, str(path)))


class LoadTest(ManifestTestCase):
    """load() refuses every malformed manifest, naming the first problem."""

    def write_raw(self, manifest: object) -> str:
        """Write manifest as JSON without validation; return its path."""
        path = self.base / "manifest.json"
        _ = path.write_text(json.dumps(manifest), encoding="utf-8")
        return str(path)

    def manifest(self) -> dict[str, object]:
        """Return a valid manifest as a plain, mutable structure."""
        return copy.deepcopy(json.loads(json.dumps(self.build())))  # pyright: ignore[reportAny]

    def assert_load_fails(self, needle: str, manifest: object) -> None:
        """Assert loading manifest fails naming needle."""
        path = self.write_raw(manifest)
        self.assert_fails(needle, lambda: load(path))

    def test_missing_key(self) -> None:
        for key in ("schema", "sha", "tar", "files"):
            with self.subTest(key=key):
                manifest = self.manifest()
                del manifest[key]
                self.assert_load_fails(f"missing key '{key}'", manifest)

    def test_missing_file_key(self) -> None:
        manifest = self.manifest()
        files = manifest["files"]
        assert isinstance(files, list)
        del files[0]["sha256"]
        self.assert_load_fails("files[0]: missing key 'sha256'", manifest)

    def test_schema_2(self) -> None:
        manifest = self.manifest()
        manifest["schema"] = 2
        self.assert_load_fails("schema", manifest)

    def test_bad_paths(self) -> None:
        for path, needle in (
            ("../x", "'..'"),
            ("a/../b", "'..'"),
            ("/etc/passwd", "absolute"),
            ("", "empty"),
            ("./a", "normalised"),
        ):
            with self.subTest(path=path):
                manifest = self.manifest()
                manifest["files"] = [{"path": path, "symlink": "x"}]
                self.assert_load_fails(needle, manifest)

    def test_bad_root(self) -> None:
        for root, needle in (
            ("", "empty"),
            ("/tmp/out", "absolute"),
            ("../outside", "'..'"),
        ):
            with self.subTest(root=root):
                manifest = self.manifest()
                manifest["root"] = root
                self.assert_load_fails(f"root: {root!r} ", manifest)
                self.assert_load_fails(needle, manifest)

    def test_unsorted_files(self) -> None:
        manifest = self.manifest()
        files = manifest["files"]
        assert isinstance(files, list)
        files.reverse()
        self.assert_load_fails("not sorted", manifest)

    def test_path_below_listed_path(self) -> None:
        manifest = self.manifest()
        manifest["files"] = [
            {"path": "link", "symlink": "/etc"},
            {"path": "link/passwd", "symlink": "x"},
        ]
        self.assert_load_fails("lies below", manifest)

    def test_bad_mode(self) -> None:
        for mode in ("8000", "07777", "644", "-001", "0x1f"):
            with self.subTest(mode=mode):
                manifest = self.manifest()
                files = manifest["files"]
                assert isinstance(files, list)
                files[0]["mode"] = mode
                self.assert_load_fails("mode", manifest)

    def test_bad_types(self) -> None:
        for key, value in (
            ("schema", True),
            ("run_id", 41830119310),
            ("created_at", "yesterday"),
            ("tar", {"path": "a/b.tar", "sha256": "0" * 64, "size": 1}),
            ("tar", {"path": "b.tar", "sha256": "0" * 63, "size": 1}),
            ("tar", {"path": "b.tar", "sha256": "0" * 64, "size": -1}),
            ("files", {}),
        ):
            with self.subTest(key=key, value=value):
                manifest = self.manifest()
                manifest[key] = value
                self.assert_load_fails(key, manifest)

    def test_not_utf8_encodable(self) -> None:
        for key, value, needle in (
            ("files", [{"path": "\ud800", "symlink": "x"}], "files[0].path"),
            ("files", [{"path": "a", "symlink": "\ud800"}], "files[0].symlink"),
            ("artifact_name", "\ud800", "artifact_name"),
        ):
            with self.subTest(key=key, needle=needle):
                manifest = self.manifest()
                manifest[key] = value
                self.assert_load_fails(needle, manifest)

    def test_unexpected_key(self) -> None:
        manifest = self.manifest()
        manifest["extra"] = "x"
        self.assert_load_fails("unexpected key 'extra'", manifest)

    def test_surrogate_key(self) -> None:
        manifest = self.manifest()
        manifest["\ud800"] = 1
        self.assert_load_fails("unexpected key '\\ud800'", manifest)

    def test_fifo_rejected(self) -> None:
        fifo = self.fifo()
        self.assert_fails("not a regular file", lambda: load(fifo))

    def test_not_json(self) -> None:
        path = self.base / "manifest.json"
        for data in (b"{", b"\xff", b'{"schema": 1, "schema": 1}', b"NaN"):
            with self.subTest(data=data):
                _ = path.write_bytes(data)
                self.assert_fails("not valid UTF-8 JSON", lambda: load(str(path)))


class VerifyTest(ManifestTestCase):
    """verify_tar() and verify_tree() refuse any change to the content."""

    def test_tar_accepted(self) -> None:
        verify_tar(self.build(), str(self.tar))

    def test_tar_changed_byte(self) -> None:
        manifest = self.build()
        data = bytearray(self.tar.read_bytes())
        data[10] ^= 1
        _ = self.tar.write_bytes(bytes(data))
        self.assert_fails("SHA-256", lambda: verify_tar(manifest, str(self.tar)))

    def test_tar_fifo(self) -> None:
        manifest = self.build()
        fifo = self.fifo()
        self.assert_fails("not a regular file", lambda: verify_tar(manifest, fifo))

    def test_tar_read_error(self) -> None:
        manifest = self.build()
        with self.read_error():
            self.assert_fails(
                "cannot read", lambda: verify_tar(manifest, str(self.tar))
            )

    def test_tar_changed_size(self) -> None:
        manifest = self.build()
        with self.tar.open("ab") as handle:
            _ = handle.write(b"x")
        self.assert_fails("size", lambda: verify_tar(manifest, str(self.tar)))

    def test_tree_accepted(self) -> None:
        self.assertEqual(verify_tree(self.build(), str(self.root)), 3)

    def test_tree_missing_file(self) -> None:
        manifest = self.build()
        (self.root / "org" / "pkg" / "a.pom").unlink()
        self.assert_fails(
            "org/pkg/a.pom", lambda: verify_tree(manifest, str(self.root))
        )

    def test_tree_changed_file(self) -> None:
        manifest = self.build()
        _ = (self.root / "org" / "pkg" / "a.pom").write_text("<Project/>\n")
        self.assert_fails("SHA-256", lambda: verify_tree(manifest, str(self.root)))

    def test_tree_changed_size(self) -> None:
        manifest = self.build()
        _ = (self.root / "org" / "pkg" / "a.pom").write_text("<project/>\n\n")
        self.assert_fails("size", lambda: verify_tree(manifest, str(self.root)))

    def test_tree_wrong_mode(self) -> None:
        manifest = self.build()
        (self.root / "bin" / "run.sh").chmod(0o644)
        self.assert_fails("mode", lambda: verify_tree(manifest, str(self.root)))

    def test_tree_changed_symlink(self) -> None:
        manifest = self.build()
        (self.root / "link").unlink()
        (self.root / "link").symlink_to("org/pkg/a.pom")
        self.assert_fails("symlink", lambda: verify_tree(manifest, str(self.root)))

    def test_tree_symlink_replaced_by_file(self) -> None:
        manifest = self.build()
        (self.root / "link").unlink()
        _ = (self.root / "link").write_text("not a link\n")
        self.assert_fails("symlink", lambda: verify_tree(manifest, str(self.root)))

    def test_tree_extra_file(self) -> None:
        manifest = self.build()
        _ = (self.root / "empty" / "extra.txt").write_text("x\n")
        self.assert_fails(
            "empty/extra.txt", lambda: verify_tree(manifest, str(self.root))
        )

    def test_tree_parent_replaced_by_symlink(self) -> None:
        manifest = self.build()
        moved = self.base / "elsewhere"
        _ = (self.root / "org").rename(moved)
        (self.root / "org").symlink_to(moved)
        self.assert_fails("org", lambda: verify_tree(manifest, str(self.root)))

    def test_tree_parent_swapped_after_listing(self) -> None:
        manifest = self.build()
        moved = self.base / "elsewhere"
        listing = cast("Callable[[str, str], Iterator[str]]", manifest_module.walk)

        def walk_then_swap(root: str, relative: str) -> Iterator[str]:
            yield from listing(root, relative)
            if not relative:
                _ = (self.root / "org").rename(moved)
                (self.root / "org").symlink_to(moved)

        with mock.patch("scripts.manifest.walk", walk_then_swap):
            self.assert_fails(
                "org: cannot open as a directory",
                lambda: verify_tree(manifest, str(self.root)),
            )

    def test_tree_invalid_root(self) -> None:
        manifest = self.build()
        manifest["files"] = []
        for root in self.invalid_roots():
            with self.subTest(root=root):
                self.assert_fails(root, partial(verify_tree, manifest, root))

    def test_tree_unreadable_dir(self) -> None:
        manifest = self.build()
        self.unreadable_dir()
        self.assert_fails(
            "empty: cannot list directory",
            lambda: verify_tree(manifest, str(self.root)),
        )

    def test_annotation_surrogate_escaped(self) -> None:
        manifest = self.build()
        if os.geteuid() == 0:
            self.skipTest("root can list any directory")
        unlistable = os.fsencode(self.root) + b"/\xff"
        os.mkdir(unlistable, 0)
        self.addCleanup(os.chmod, unlistable, 0o755)
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(ManifestError) as caught:
            _ = verify_tree(manifest, str(self.root))
        self.assertIn("\udcff: cannot list directory", str(caught.exception))
        self.assertTrue(
            out.getvalue().startswith("::error::\\udcff: cannot list directory")
        )

    def test_annotation_escaped(self) -> None:
        manifest = self.build()
        injected = "zz%\r\n::warning::injected"
        manifest["files"].append({"path": injected, "symlink": "x"})
        path = self.base / "manifest.json"
        _ = write(manifest, str(path))
        loaded = load(str(path))
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(ManifestError) as caught:
            _ = verify_tree(loaded, str(self.root))
        suffix = ": listed in the manifest but missing"
        self.assertEqual(str(caught.exception), injected + suffix)
        self.assertEqual(
            out.getvalue(),
            f"::error::zz%25%0D%0A::warning::injected{suffix}\n",
        )


if __name__ == "__main__":
    _ = unittest.main()
