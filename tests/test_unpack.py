# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/unpack.py.

Each test prepares a scratch directory as if actions/download-artifact
had filled it, runs the unpack step against a throwaway workspace and
checks its exit code, its '::error::' lines and what it left on disk.
"""

from __future__ import annotations

import hashlib
import io
import os
import resource
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager, redirect_stdout
from pathlib import Path
from typing import cast, final
from unittest import mock

import scripts.unpack as unpack
from scripts.manifest import Manifest, build, write
from scripts.manifest_schema import FileEntry

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "unpack.py"
NAME = "maven-m2repo"
RUN_ID = "41830119310"
ENV = {
    "GITHUB_RUN_ID": RUN_ID,
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_REPOSITORY": "onap/cps",
    "GITHUB_SHA": "32250c531" + "0" * 31,
}
CREATED_AT = "2026-11-04T12:00:00Z"
TAR_NAME = "build-artifact.tar"
MANIFEST_NAME = "build-artifact.manifest.json"
BLOCK = tarfile.BLOCKSIZE
DEPTH_LIMIT = 256
OPEN_FILES_LIMIT = 64
# Before Python 3.13, os.path.realpath recursed once per symlink.
REALPATH_RECURSES = sys.version_info < (3, 13)

Member = tuple[tarfile.TarInfo, bytes | None]


def regular(name: str, data: bytes, mode: int = 0o644) -> Member:
    """Return a regular tar member holding data."""
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    return info, data


def special(name: str, kind: bytes, linkname: str = "") -> Member:
    """Return a tar member of type kind with no data."""
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
    return info, None


def entry_for(name: str, data: bytes, mode: int = 0o644) -> FileEntry:
    """Return the manifest entry for a regular file."""
    return {
        "path": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mode": f"{mode:04o}",
    }


def reading_recurses(path: Path) -> bool:
    """Return whether tarfile raises RecursionError reading the tar at path."""
    try:
        with tarfile.open(path, "r:") as archive:
            _ = archive.getmembers()
    except RecursionError:
        return True
    return False


class UnpackTestCase(unittest.TestCase):
    """Run the unpack step against a throwaway workspace and download."""

    destination: str = "unpacked"
    expected_digest: str = ""
    stdout: str = ""
    _base: Path | None = None

    @property
    def base(self) -> Path:
        """Return a temporary directory for this test."""
        if self._base is None:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            self._base = Path(os.path.realpath(tmp.name))
        return self._base

    def subdirectory(self, name: str) -> Path:
        """Return base/name, creating it once."""
        path = self.base / name
        path.mkdir(exist_ok=True)
        return path

    @property
    def workspace(self) -> Path:
        """Return the stand-in for GITHUB_WORKSPACE."""
        return self.subdirectory("workspace")

    @property
    def download(self) -> Path:
        """Return the scratch directory the artefact was downloaded into."""
        return self.subdirectory("download")

    @property
    def output(self) -> Path:
        """Return the stand-in for GITHUB_OUTPUT."""
        return self.base / "github_output"

    @property
    def summary(self) -> Path:
        """Return the stand-in for GITHUB_STEP_SUMMARY."""
        return self.base / "step_summary"

    @property
    def target(self) -> Path:
        """Return where destination resolves in the workspace."""
        return self.workspace / self.destination

    def write_tar(self, members: list[Member]) -> Path:
        """Write members, in order, as the downloaded tar."""
        path = self.download / TAR_NAME
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
            for info, data in members:
                archive.addfile(info, None if data is None else io.BytesIO(data))
        return path

    def manifest_for(self, files: list[FileEntry]) -> Manifest:
        """Return a manifest listing files, matching the downloaded tar."""
        empty = self.subdirectory("empty")
        manifest = build(
            str(empty),
            ["."],
            artifact_name=NAME,
            tar_path=str(self.download / TAR_NAME),
            env=ENV,
            created_at=CREATED_AT,
        )
        manifest["files"] = sorted(files, key=lambda item: item["path"])
        return manifest

    def write_manifest(self, manifest: Manifest) -> str:
        """Write manifest into the download; return its digest."""
        return write(manifest, str(self.download / MANIFEST_NAME))

    def download_of(self, members: list[Member], files: list[FileEntry]) -> str:
        """Write a tar and its manifest into the download; return the digest."""
        _ = self.write_tar(members)
        return self.write_manifest(self.manifest_for(files))

    def packed_tree(self) -> str:
        """Pack a small tree as the producer would; return the digest."""
        root = self.base / "m2repo"
        (root / "bin").mkdir(parents=True)
        (root / "org" / "pkg").mkdir(parents=True)
        (root / "empty").mkdir()
        _ = (root / "bin" / "run.sh").write_text("#!/bin/sh\necho run\n")
        _ = (root / "org" / "pkg" / "a.pom").write_text("<project/>\n")
        _ = (root / "shared.txt").write_text("shared\n")
        (root / "bin" / "run.sh").chmod(0o755)
        (root / "org" / "pkg" / "a.pom").chmod(0o644)
        (root / "shared.txt").chmod(0o664)
        (root / "link").symlink_to("bin/run.sh")
        os.link(root / "shared.txt", root / "shared-link.txt")
        tar = self.download / TAR_NAME
        with tarfile.open(tar, "w", format=tarfile.PAX_FORMAT) as archive:
            for name in sorted(os.listdir(root)):
                archive.add(root / name, arcname=name)
        manifest = build(
            str(root),
            ["."],
            artifact_name=NAME,
            tar_path=str(tar),
            env=ENV,
            created_at=CREATED_AT,
        )
        return self.write_manifest(manifest)

    def directory_download(self) -> None:
        """Write a download whose directories have non-default modes."""
        data = b"data\n"
        modes = {"a": 0o700, "a/b": 0o555, "c": 0o000, "d": 0o3775}
        members = [special(name, tarfile.DIRTYPE) for name in modes]
        for info, _ in members:
            info.mode = modes[info.name]
        members.append(regular("a/b/f", data))
        _ = self.download_of(members, [entry_for("a/b/f", data)])

    def assert_directory_modes(self) -> None:
        """Assert the directories of directory_download kept their modes."""
        for name, mode in (("a", 0o700), ("a/b", 0o555), ("c", 0o000), ("d", 0o775)):
            actual = stat.S_IMODE((self.target / name).lstat().st_mode)
            self.assertEqual(actual, mode, name)

    def environment(self, **overrides: str) -> dict[str, str]:
        """Return the step's environment, with overrides applied."""
        env = {
            "GITHUB_WORKSPACE": str(self.workspace),
            "GITHUB_RUN_ID": RUN_ID,
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "INPUT_ARTIFACT_NAME": NAME,
            "INPUT_DESTINATION": self.destination,
            "INPUT_EXPECTED_DIGEST": self.expected_digest,
            "DOWNLOAD_DIR": str(self.download),
        }
        env.update(overrides)
        return env

    def run_unpack(self, **overrides: str) -> int:
        """Run the unpack step; keep its stdout; return its exit code."""
        env = self.environment(**overrides)
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(buffer):
            code = unpack.main()
        self.stdout = buffer.getvalue()
        return code

    def outputs(self) -> dict[str, str]:
        """Parse the heredoc-form outputs the step wrote."""
        lines = self.output.read_text(encoding="utf-8").splitlines()
        values: dict[str, str] = {}
        index = 0
        while index < len(lines):
            key, delimiter = lines[index].split("<<", 1)
            end = lines.index(delimiter, index + 1)
            values[key] = "\n".join(lines[index + 1 : end])
            index = end + 1
        return values

    def assert_refused(self, message: str, **overrides: str) -> None:
        """Assert the step fails naming message and leaves no destination."""
        self.assertEqual(self.run_unpack(**overrides), 1)
        errors = [
            line for line in self.stdout.splitlines() if line.startswith("::error::")
        ]
        self.assertTrue(errors, self.stdout)
        self.assertIn(message, errors[0])
        self.assertFalse(os.path.lexists(self.target))


@final
class HappyPathTest(UnpackTestCase):
    """A packed tree extracts intact and reports its outputs."""

    def test_extracts_tree(self) -> None:
        digest = self.packed_tree()
        self.assertEqual(self.run_unpack(), 0, self.stdout)
        target = self.target
        self.assertEqual(
            self.outputs(),
            {
                "destination": str(target),
                "artifact-name": NAME,
                "digest": digest,
                "file-count": "5",
            },
        )
        self.assertEqual(
            stat.S_IMODE((target / "bin" / "run.sh").stat().st_mode), 0o755
        )
        self.assertEqual(stat.S_IMODE((target / "shared.txt").stat().st_mode), 0o664)
        self.assertEqual((target / "shared-link.txt").read_text(), "shared\n")
        self.assertEqual(os.readlink(target / "link"), "bin/run.sh")
        self.assertTrue((target / "empty").is_dir())
        summary = self.summary.read_text(encoding="utf-8")
        self.assertIn(NAME, summary)
        self.assertIn(digest, summary)
        self.assertIn("5 files", summary)

    def test_runs_as_isolated_script(self) -> None:
        _ = self.packed_tree()
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT)],
            env=self.environment(),
            cwd=self.base,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.target / "bin" / "run.sh").is_file())

    def test_expected_digest_matches(self) -> None:
        self.expected_digest = self.packed_tree()
        self.assertEqual(self.run_unpack(), 0, self.stdout)

    def test_creates_missing_parents(self) -> None:
        _ = self.packed_tree()
        self.destination = "out/deep/unpacked"
        self.assertEqual(self.run_unpack(), 0, self.stdout)
        self.assertTrue((self.target / "bin" / "run.sh").is_file())

    def test_destination_with_leading_dot(self) -> None:
        _ = self.packed_tree()
        self.destination = "./unpacked"
        self.assertEqual(self.run_unpack(), 0, self.stdout)
        self.assertEqual(
            self.outputs()["destination"], str(self.workspace / "unpacked")
        )
        self.assertTrue((self.target / "bin" / "run.sh").is_file())

    def test_destination_with_inner_dot(self) -> None:
        _ = self.packed_tree()
        self.destination = "out/./unpacked"
        self.assertEqual(self.run_unpack(), 0, self.stdout)
        self.assertEqual(
            self.outputs()["destination"], str(self.workspace / "out" / "unpacked")
        )
        self.assertTrue((self.target / "bin" / "run.sh").is_file())

    def test_extractall_uses_data_filter(self) -> None:
        _ = self.packed_tree()
        original = tarfile.TarFile.extractall
        calls: list[dict[str, object]] = []

        def spy(archive: tarfile.TarFile, *args: object, **kwargs: object) -> None:
            calls.append(dict(kwargs))
            extract = cast("Callable[..., None]", original)
            extract(archive, *args, **kwargs)

        with mock.patch.object(tarfile.TarFile, "extractall", spy):
            self.assertEqual(self.run_unpack(), 0, self.stdout)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("filter"), "data")
        self.assertIs(calls[0].get("numeric_owner"), False)

    def test_fallback_without_data_filter(self) -> None:
        _ = self.packed_tree()
        with mock.patch.object(unpack, "has_data_filter", return_value=False):
            self.assertEqual(self.run_unpack(), 0, self.stdout)
        target = self.target
        self.assertEqual(
            stat.S_IMODE((target / "bin" / "run.sh").stat().st_mode), 0o755
        )
        self.assertEqual(os.readlink(target / "link"), "bin/run.sh")

    def test_directory_modes_restored(self) -> None:
        self.directory_download()
        self.assertEqual(self.run_unpack(), 0, self.stdout)
        self.assert_directory_modes()
        self.assertEqual(self.outputs()["file-count"], "1")

    def test_directory_modes_restored_fallback(self) -> None:
        self.directory_download()
        with mock.patch.object(unpack, "has_data_filter", return_value=False):
            self.assertEqual(self.run_unpack(), 0, self.stdout)
        self.assert_directory_modes()

    def test_special_bits_dropped(self) -> None:
        data = b"#!/bin/sh\n"
        files = [entry_for("tool", data, 0o4755), entry_for("dir-bits", data, 0o3644)]
        members = [regular("tool", data, 0o4755), regular("dir-bits", data, 0o3644)]
        _ = self.download_of(members, files)
        self.assertEqual(self.run_unpack(), 0, self.stdout)
        self.assertEqual(stat.S_IMODE((self.target / "tool").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.target / "dir-bits").stat().st_mode), 0o644)

    def test_owner_unreadable_file(self) -> None:
        data = b"secret\n"
        link, _ = special("secret-link", tarfile.LNKTYPE, "secret")
        link.mode = 0o044
        members = [regular("secret", data, 0o044), (link, None)]
        files = [
            entry_for("secret", data, 0o044),
            entry_for("secret-link", data, 0o044),
        ]
        _ = self.download_of(members, files)
        self.assertEqual(self.run_unpack(), 0, self.stdout)
        for name in ("secret", "secret-link"):
            mode = stat.S_IMODE((self.target / name).stat().st_mode)
            self.assertEqual(mode, 0o044, name)
        self.assertEqual(self.outputs()["file-count"], "2")

    def test_owner_unreadable_files_within_descriptor_limit(self) -> None:
        data = b"secret\n"
        members: list[Member] = []
        files: list[FileEntry] = []
        names: list[str] = []
        for index in range(OPEN_FILES_LIMIT * 2):
            name = f"secret{index}"
            members.append(regular(name, data, 0o044))
            files.append(entry_for(name, data, 0o044))
            names.append(name)
            if index % 2:
                link, _ = special(f"{name}-link", tarfile.LNKTYPE, name)
                link.mode = 0o044
                members.append((link, None))
                files.append(entry_for(f"{name}-link", data, 0o044))
                names.append(f"{name}-link")
        _ = self.download_of(members, files)
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (OPEN_FILES_LIMIT, hard))
        try:
            code = self.run_unpack()
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        self.assertEqual(code, 0, self.stdout)
        self.assertEqual(self.outputs()["file-count"], str(len(names)))
        for name in names:
            mode = stat.S_IMODE((self.target / name).stat().st_mode)
            self.assertEqual(mode, 0o044, name)

    def dot_link_download(self) -> dict[str, str]:
        """Download symlinks whose targets hold '.' or empty segments."""
        data = b"#!/bin/sh\n"
        links = {
            "dot": "./bin/run.sh",
            "inner": "bin/./run.sh",
            "double": "bin//run.sh",
            "trailing": "bin/",
            "sub/up": "./../bin/run.sh",
        }
        members = [
            special("bin", tarfile.DIRTYPE),
            regular("bin/run.sh", data, 0o755),
            special("sub", tarfile.DIRTYPE),
        ]
        members += [
            special(name, tarfile.SYMTYPE, target) for name, target in links.items()
        ]
        files: list[FileEntry] = [entry_for("bin/run.sh", data, 0o755)]
        for name, target in links.items():
            files.append({"path": name, "symlink": target})
        _ = self.download_of(members, files)
        return links

    def test_symlink_targets_with_dot_segments(self) -> None:
        links = self.dot_link_download()
        self.assertEqual(self.run_unpack(), 0, self.stdout)
        for name, target in links.items():
            self.assertEqual(os.readlink(self.target / name), target, name)

    def test_symlink_targets_with_dot_segments_fallback(self) -> None:
        links = self.dot_link_download()
        with mock.patch.object(unpack, "has_data_filter", return_value=False):
            self.assertEqual(self.run_unpack(), 0, self.stdout)
        for name, target in links.items():
            self.assertEqual(os.readlink(self.target / name), target, name)

    def test_nesting_at_depth_limit(self) -> None:
        data = b"deep\n"
        name = "/".join(["d"] * (DEPTH_LIMIT - 1) + ["f"])
        _ = self.download_of([regular(name, data)], [entry_for(name, data)])
        self.assertEqual(self.run_unpack(), 0, self.stdout)
        self.assertEqual((self.target / name).read_bytes(), data)


@final
class DestinationTest(UnpackTestCase):
    """The extraction root must be new and inside the workspace."""

    def test_existing_empty_destination(self) -> None:
        _ = self.packed_tree()
        self.target.mkdir()
        self.assertEqual(self.run_unpack(), 1)
        self.assertIn("must not exist", self.stdout)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_symlinked_ancestor(self) -> None:
        _ = self.packed_tree()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.workspace / "linked").symlink_to(elsewhere)
        self.destination = "linked/unpacked"
        self.assertEqual(self.run_unpack(), 1)
        self.assertIn("symlink", self.stdout)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_destination_normalising_to_workspace(self) -> None:
        _ = self.packed_tree()
        self.destination = "./."
        self.assertEqual(self.run_unpack(), 1)
        self.assertIn("workspace itself", self.stdout)
        self.assertEqual(list(self.workspace.iterdir()), [])


@final
class DownloadTest(UnpackTestCase):
    """The download holds exactly the tar and a manifest that match."""

    def test_extra_download_file(self) -> None:
        _ = self.packed_tree()
        _ = (self.download / "extra.txt").write_text("extra\n")
        self.assert_refused("download must hold exactly")

    def test_tar_sha256_mismatch(self) -> None:
        _ = self.packed_tree()
        tar = self.download / TAR_NAME
        data = bytearray(tar.read_bytes())
        data[-1] ^= 0xFF
        _ = tar.write_bytes(bytes(data))
        self.assert_refused("SHA-256 does not match the manifest")

    def test_tar_size_mismatch(self) -> None:
        _ = self.packed_tree()
        with (self.download / TAR_NAME).open("ab") as handle:
            _ = handle.write(b"\0" * 512)
        self.assert_refused("does not match manifest")

    def test_run_id_mismatch(self) -> None:
        _ = self.packed_tree()
        self.assert_refused("artefact comes from another run", GITHUB_RUN_ID="1")

    def test_expected_digest_mismatch(self) -> None:
        _ = self.packed_tree()
        self.expected_digest = "0" * 64
        self.assert_refused("expected-digest")

    def test_artifact_name_mismatch(self) -> None:
        _ = self.packed_tree()
        self.assert_refused("artifact_name", INPUT_ARTIFACT_NAME="other")


@final
class MemberTest(UnpackTestCase):
    """Every tar member is checked before anything is extracted."""

    def refuse_members(self, members: list[Member], message: str) -> None:
        """Assert a download of members, listed in no manifest, is refused."""
        _ = self.download_of(members, [])
        self.assert_refused(message)

    def test_absolute_member(self) -> None:
        self.refuse_members([regular("/tmp/evil", b"x")], "is absolute")

    def test_dotdot_member(self) -> None:
        self.refuse_members([regular("../evil", b"x")], "'..' segment")

    def test_symlink_to_etc(self) -> None:
        self.refuse_members(
            [special("etc", tarfile.SYMTYPE, "/etc")], "symlink target is absolute"
        )

    def test_symlink_to_parent_then_member(self) -> None:
        members = [special("a", tarfile.SYMTYPE, ".."), regular("a/x", b"x")]
        self.refuse_members(members, "symlink target leaves the root")

    def test_member_through_symlink(self) -> None:
        members = [
            special("sub", tarfile.DIRTYPE),
            special("a", tarfile.SYMTYPE, "sub"),
            regular("a/x", b"x"),
        ]
        self.refuse_members(members, "written through symlink 'a'")

    def test_symlink_climbing_too_far(self) -> None:
        members = [
            special("d", tarfile.DIRTYPE),
            special("d/l", tarfile.SYMTYPE, "../../etc"),
        ]
        self.refuse_members(members, "symlink target leaves the root")

    def test_symlink_dotdot_after_segment(self) -> None:
        members = [
            special("d", tarfile.DIRTYPE),
            special("d/l", tarfile.SYMTYPE, ".."),
            special("x", tarfile.SYMTYPE, "d/l/.."),
        ]
        self.refuse_members(members, "symlink target has '..' after a named segment")

    def test_symlink_dot_then_climbing_too_far(self) -> None:
        members = [special("x", tarfile.SYMTYPE, "./..")]
        self.refuse_members(members, "symlink target leaves the root")

    def test_nesting_beyond_depth_limit(self) -> None:
        name = "/".join(["d"] * DEPTH_LIMIT + ["f"])
        self.refuse_members([regular(name, b"x")], "nested deeper than 256 levels")

    def test_hard_link_outside(self) -> None:
        self.refuse_members(
            [special("passwd", tarfile.LNKTYPE, "/etc/passwd")], "hard link"
        )

    def test_hard_link_mode_differs(self) -> None:
        data = b"secret\n"
        link, _ = special("secret-link", tarfile.LNKTYPE, "secret")
        link.mode = 0o000
        members = [regular("secret", data, 0o400), (link, None)]
        files = [
            entry_for("secret", data, 0o400),
            entry_for("secret-link", data, 0o000),
        ]
        _ = self.download_of(members, files)
        self.assert_refused("hard link mode differs from 'secret'")

    def test_malformed_sparse_member(self) -> None:
        data = b"sparse\n"
        member, _ = regular("sparse", data)
        member.pax_headers = {"GNU.sparse.map": "not-an-integer"}
        _ = self.download_of([(member, data)], [entry_for("sparse", data)])
        self.assert_refused("cannot read as an uncompressed tar")

    def test_chained_pax_headers(self) -> None:
        data = b"shallow\n"
        info, _ = regular("shallow", data)
        payload = b"12 comment=\n"
        header = tarfile.TarInfo("././@PaxHeader")
        header.type = tarfile.XHDTYPE
        header.size = len(payload)
        record = header.tobuf(tarfile.USTAR_FORMAT) + payload.ljust(BLOCK, b"\0")
        chain = record * (sys.getrecursionlimit() + 100)
        member = info.tobuf(tarfile.USTAR_FORMAT) + data.ljust(BLOCK, b"\0")
        tar = self.download / TAR_NAME
        _ = tar.write_bytes(chain + member + b"\0" * (2 * BLOCK))
        if not reading_recurses(tar):
            self.skipTest("tarfile reads chained headers without recursing")
        _ = self.write_manifest(self.manifest_for([entry_for("shallow", data)]))
        self.assert_refused("header chain too long")

    def test_device_member(self) -> None:
        self.refuse_members([special("null", tarfile.CHRTYPE)], "device or FIFO member")

    def test_member_missing_from_manifest(self) -> None:
        data = b"listed\n"
        members = [regular("listed", data), regular("unlisted", b"x")]
        _ = self.download_of(members, [entry_for("listed", data)])
        self.assert_refused("not listed in the manifest")

    def test_mode_mismatch(self) -> None:
        data = b"listed\n"
        files = [entry_for("listed", data, 0o644)]
        _ = self.download_of([regular("listed", data, 0o755)], files)
        self.assert_refused("mode does not match the manifest")

    def test_manifest_entry_missing_from_tar(self) -> None:
        data = b"listed\n"
        files = [entry_for("listed", data), entry_for("missing", b"x")]
        _ = self.download_of([regular("listed", data)], files)
        self.assert_refused("missing from the tar")


@final
class AfterExtractionTest(UnpackTestCase):
    """A tree that fails verification after extraction is removed."""

    def test_unlisted_file_after_extraction(self) -> None:
        _ = self.packed_tree()
        self.destination = "out/unpacked"
        original = tarfile.TarFile.extractall

        def plant(archive: tarfile.TarFile, path: str, **kwargs: object) -> None:
            extract = cast("Callable[..., None]", original)
            extract(archive, path, **kwargs)
            _ = (Path(path) / "empty" / "planted.txt").write_text("planted\n")

        with mock.patch.object(tarfile.TarFile, "extractall", plant):
            self.assert_refused("present but not listed in the manifest")
        self.assertFalse(os.path.lexists(self.workspace / "out"))

    def nul_link_download(self) -> None:
        """Download a file, then a symlink whose PAX target holds a NUL."""
        data = b"first\n"
        target = "first.txt\0x"
        link, _ = special("link", tarfile.SYMTYPE, "first.txt")
        link.pax_headers = {"linkpath": target}
        files: list[FileEntry] = [
            entry_for("first.txt", data),
            {"path": "link", "symlink": target},
        ]
        _ = self.download_of([regular("first.txt", data), (link, None)], files)

    def test_symlink_target_with_nul(self) -> None:
        self.nul_link_download()
        self.assert_refused("symlink target contains a NUL byte")

    def test_extraction_value_error_removes_destination(self) -> None:
        self.nul_link_download()
        self.destination = "out/unpacked"
        with mock.patch("scripts.unpack_members.check_link_target"):
            self.assert_refused("extraction failed")
        self.assertFalse(os.path.lexists(self.workspace / "out"))

    def test_pax_mtime_overflow_removes_destination(self) -> None:
        first, second = b"first\n", b"second\n"
        late, _ = regular("second.txt", second)
        late.pax_headers = {"mtime": "inf"}
        files = [entry_for("first.txt", first), entry_for("second.txt", second)]
        _ = self.download_of([regular("first.txt", first), (late, second)], files)
        self.destination = "out/unpacked"
        self.assert_refused("extraction failed")
        self.assertFalse(os.path.lexists(self.workspace / "out"))

    def symlink_chain_download(self) -> None:
        """Download a file and a flat chain of symlinks, each to the last."""
        data = b"end\n"
        members = [regular("file", data)]
        files: list[FileEntry] = [entry_for("file", data)]
        previous = "file"
        for index in range(sys.getrecursionlimit() + 100):
            name = f"link{index}"
            members.append(special(name, tarfile.SYMTYPE, previous))
            files.append({"path": name, "symlink": previous})
            previous = name
        _ = self.download_of(members, files)
        self.destination = "out/unpacked"

    @unittest.skipUnless(REALPATH_RECURSES, "realpath resolves links iteratively")
    def test_long_symlink_chain_removes_destination(self) -> None:
        self.symlink_chain_download()
        self.assert_refused("symlink chain too long to resolve")
        self.assertFalse(os.path.lexists(self.workspace / "out"))

    @unittest.skipUnless(REALPATH_RECURSES, "realpath resolves links iteratively")
    def test_long_symlink_chain_fallback_removes_destination(self) -> None:
        self.symlink_chain_download()
        with mock.patch.object(unpack, "has_data_filter", return_value=False):
            self.assert_refused("symlink chain too long to resolve")
        self.assertFalse(os.path.lexists(self.workspace / "out"))

    def test_unexpected_error_removes_destination(self) -> None:
        _ = self.packed_tree()
        self.destination = "out/unpacked"
        boom = mock.Mock(side_effect=RuntimeError("boom"))
        with (
            mock.patch.object(unpack, "restore_modes", boom),
            self.assertRaises(RuntimeError),
        ):
            _ = self.run_unpack()
        boom.assert_called_once()
        self.assertFalse(os.path.lexists(self.workspace / "out"))

    def test_output_write_failure_removes_destination(self) -> None:
        _ = self.packed_tree()
        self.destination = "out/unpacked"
        self.assert_refused(
            "cannot write the step outputs", GITHUB_OUTPUT=str(self.base)
        )
        self.assertFalse(os.path.lexists(self.workspace / "out"))

    def test_failure_removes_restricted_directories(self) -> None:
        self.directory_download()
        self.destination = "out/unpacked"
        self.assert_refused(
            "cannot write the step outputs", GITHUB_OUTPUT=str(self.base)
        )
        self.assertFalse(os.path.lexists(self.workspace / "out"))

    def test_summary_write_failure_removes_destination(self) -> None:
        _ = self.packed_tree()
        self.destination = "out/unpacked"
        self.assert_refused(
            "cannot write the step outputs or summary",
            GITHUB_STEP_SUMMARY=str(self.base),
        )
        self.assertFalse(os.path.lexists(self.workspace / "out"))


def listing(root: Path) -> dict[str, tuple[str, int, str]]:
    """Return each path under root with its type, mode and content or target."""
    found: dict[str, tuple[str, int, str]] = {}
    for current, dirs, names in os.walk(root):
        for name in [*dirs, *names]:
            path = Path(current) / name
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                found[str(path.relative_to(root))] = ("link", mode, os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                found[str(path.relative_to(root))] = ("dir", mode, "")
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                found[str(path.relative_to(root))] = ("file", mode, digest)
    return found


@final
class OverlayTest(UnpackTestCase):
    """overlay: 'true' adds files to an existing directory, replacing nothing."""

    destination: str = "checkout"

    def existing_tree(self) -> None:
        """Fill the destination with files the artefact must not touch."""
        (self.target / "bin").mkdir(parents=True)
        _ = (self.target / "Dockerfile").write_text("FROM scratch\n")
        _ = (self.target / "bin" / "build.sh").write_text("#!/bin/sh\n")
        (self.target / "bin").chmod(0o750)

    def run_overlay(self, **overrides: str) -> int:
        """Run the unpack step with overlay set, unless overrides set it."""
        return self.run_unpack(**{"INPUT_OVERLAY": "true", **overrides})

    def assert_overlay_refused(self, message: str, **overrides: str) -> None:
        """Assert overlay fails naming message and leaves the destination as it was."""
        before = listing(self.workspace)
        self.assertEqual(self.run_overlay(**overrides), 1, self.stdout)
        errors = [
            line for line in self.stdout.splitlines() if line.startswith("::error::")
        ]
        self.assertTrue(errors, self.stdout)
        self.assertIn(message, errors[0])
        self.assertEqual(listing(self.workspace), before)

    def test_adds_files(self) -> None:
        digest = self.packed_tree()
        self.existing_tree()
        before = listing(self.target)
        self.assertEqual(self.run_overlay(), 0, self.stdout)
        self.assertEqual(
            self.outputs(),
            {
                "destination": str(self.target),
                "artifact-name": NAME,
                "digest": digest,
                "file-count": "5",
            },
        )
        after = listing(self.target)
        for path in ("Dockerfile", "bin/build.sh"):
            self.assertEqual(after[path], before[path], path)
        self.assertEqual(
            (self.target / "bin" / "run.sh").read_text(), "#!/bin/sh\necho run\n"
        )
        self.assertEqual(
            (self.target / "org" / "pkg" / "a.pom").read_text(), "<project/>\n"
        )
        self.assertEqual(os.readlink(self.target / "link"), "bin/run.sh")
        self.assertTrue((self.target / "empty").is_dir())
        self.assertEqual(
            sorted(os.listdir(self.target)),
            [
                "Dockerfile",
                "bin",
                "empty",
                "link",
                "org",
                "shared-link.txt",
                "shared.txt",
            ],
        )

    def test_adds_files_fallback(self) -> None:
        _ = self.packed_tree()
        self.existing_tree()
        with mock.patch.object(unpack, "has_data_filter", return_value=False):
            self.assertEqual(self.run_overlay(), 0, self.stdout)
        self.assertTrue((self.target / "bin" / "run.sh").is_file())
        self.assertEqual((self.target / "Dockerfile").read_text(), "FROM scratch\n")

    def test_into_workspace(self) -> None:
        _ = self.packed_tree()
        self.destination = "."
        _ = (self.workspace / "Dockerfile").write_text("FROM scratch\n")
        self.assertEqual(self.run_overlay(), 0, self.stdout)
        self.assertEqual(self.outputs()["destination"], str(self.workspace))
        self.assertTrue((self.workspace / "bin" / "run.sh").is_file())
        self.assertEqual((self.workspace / "Dockerfile").read_text(), "FROM scratch\n")

    def test_existing_directory_keeps_mode(self) -> None:
        self.directory_download()
        (self.target / "a").mkdir(parents=True, mode=0o750)
        (self.target / "a").chmod(0o750)
        self.assertEqual(self.run_overlay(), 0, self.stdout)
        self.assertEqual(stat.S_IMODE((self.target / "a").lstat().st_mode), 0o750)
        for name, mode in (("a/b", 0o555), ("c", 0o000), ("d", 0o775)):
            actual = stat.S_IMODE((self.target / name).lstat().st_mode)
            self.assertEqual(actual, mode, name)
        self.assertEqual((self.target / "a" / "b" / "f").read_bytes(), b"data\n")

    def test_onto_existing_file(self) -> None:
        _ = self.packed_tree()
        self.existing_tree()
        _ = (self.target / "shared.txt").write_text("mine\n")
        self.assert_overlay_refused("would replace existing file 'shared.txt'")

    def test_onto_existing_directory(self) -> None:
        _ = self.packed_tree()
        self.existing_tree()
        (self.target / "shared.txt").mkdir()
        self.assert_overlay_refused("would replace existing directory 'shared.txt'")

    def test_onto_existing_symlink(self) -> None:
        _ = self.packed_tree()
        self.existing_tree()
        (self.target / "shared.txt").symlink_to("Dockerfile")
        self.assert_overlay_refused("would replace existing symlink 'shared.txt'")

    def test_directory_member_onto_existing_file(self) -> None:
        _ = self.packed_tree()
        self.existing_tree()
        _ = (self.target / "empty").write_text("mine\n")
        self.assert_overlay_refused("would replace existing file 'empty'")

    def nested_download(self) -> None:
        """Download one file two levels deep, with no directory members."""
        data = b"nested\n"
        name = "bin/sub/run.sh"
        _ = self.download_of([regular(name, data)], [entry_for(name, data)])

    def test_below_existing_file(self) -> None:
        self.nested_download()
        self.target.mkdir()
        _ = (self.target / "bin").write_text("mine\n")
        self.assert_overlay_refused("lies below existing file 'bin'")

    def test_through_symlink_outside(self) -> None:
        self.nested_download()
        self.target.mkdir()
        outside = self.subdirectory("outside")
        (self.target / "bin").symlink_to(outside)
        self.assert_overlay_refused("would be written through symlink 'bin'")
        self.assertEqual(list(outside.iterdir()), [])

    def test_through_symlink_inside(self) -> None:
        self.nested_download()
        (self.target / "real").mkdir(parents=True)
        (self.target / "bin").symlink_to("real")
        self.assert_overlay_refused("would be written through symlink 'bin'")
        self.assertEqual(list((self.target / "real").iterdir()), [])

    def test_symlink_target_escapes_through_existing_symlink(self) -> None:
        self.target.mkdir()
        outside = self.subdirectory("outside")
        (self.target / "ext").symlink_to(outside)
        members = [special("link", tarfile.SYMTYPE, "ext/secret")]
        _ = self.download_of(members, [{"path": "link", "symlink": "ext/secret"}])
        self.assert_overlay_refused("resolves outside the destination")

    def link_download(self, links: dict[str, str]) -> None:
        """Download an artefact holding only the symlinks in links."""
        members = [
            special(name, tarfile.SYMTYPE, target) for name, target in links.items()
        ]
        files: list[FileEntry] = [
            {"path": name, "symlink": target} for name, target in links.items()
        ]
        _ = self.download_of(members, files)

    def test_symlink_chain_through_new_then_existing_link(self) -> None:
        self.target.mkdir()
        (self.target / "ext").symlink_to(self.subdirectory("outside"))
        self.link_download({"jump": ".", "link": "jump/ext/secret"})
        self.assert_overlay_refused("'link': symlink target resolves outside")

    def test_symlink_chain_through_existing_then_new_link(self) -> None:
        self.target.mkdir()
        (self.target / "ext").symlink_to(self.subdirectory("outside"))
        (self.target / "hop").symlink_to("jump")
        self.link_download({"jump": ".", "link": "hop/ext/secret"})
        self.assert_overlay_refused("'link': symlink target resolves outside")

    def test_symlink_chain_inside_both_trees(self) -> None:
        (self.target / "real").mkdir(parents=True)
        (self.target / "in").symlink_to("real")
        self.link_download({"jump": ".", "link": "jump/in"})
        self.assertEqual(self.run_overlay(), 0, self.stdout)
        self.assertEqual(os.readlink(self.target / "link"), "jump/in")
        self.assertEqual((self.target / "link").resolve(), self.target / "real")

    def test_symlink_chain_through_absolute_link_inside(self) -> None:
        (self.target / "real").mkdir(parents=True)
        (self.target / "abs").symlink_to(self.target.resolve() / "real")
        self.link_download({"jump": ".", "link": "jump/abs"})
        self.assertEqual(self.run_overlay(), 0, self.stdout)
        self.assertEqual((self.target / "link").resolve(), self.target / "real")

    def test_symlink_loop(self) -> None:
        self.target.mkdir()
        (self.target / "loop").symlink_to("link")
        self.link_download({"link": "loop/x"})
        self.assert_overlay_refused("'link': symlink chain too long to resolve")

    def files_download(self, names: list[str]) -> None:
        """Download an artefact holding a small file at each of names."""
        members = [regular(name, name.encode()) for name in names]
        files = [entry_for(name, name.encode()) for name in names]
        _ = self.download_of(members, files)

    def test_symlink_chain_through_case_alias(self) -> None:
        self.target.mkdir()
        (self.target / "ext").symlink_to(self.subdirectory("outside"))
        self.link_download({"Jump": ".", "link": "jump/ext/secret"})
        self.assert_overlay_refused(
            "'link': symlink target 'jump' and 'Jump' differ only by case"
        )

    def test_members_differing_by_case(self) -> None:
        self.target.mkdir()
        self.files_download(["a/Readme.md", "a/README.md"])
        self.assert_overlay_refused(
            "'a/README.md' and 'a/Readme.md' differ only by case or Unicode"
        )

    def test_member_prefixes_differing_by_case(self) -> None:
        self.target.mkdir()
        self.files_download(["Dir/a", "dir/b"])
        self.assert_overlay_refused("'dir' and 'Dir' differ only by case")

    def test_members_differing_by_normalisation(self) -> None:
        self.target.mkdir()
        composed = unicodedata.normalize("NFC", "café")
        decomposed = unicodedata.normalize("NFD", "café")
        self.files_download([composed, decomposed])
        self.assert_overlay_refused(
            f"{decomposed!r} and {composed!r} differ only by case or Unicode"
        )

    def test_member_beside_existing_case_alias(self) -> None:
        self.target.mkdir()
        _ = (self.target / "README.md").write_text("mine\n")
        self.files_download(["Readme.md"])
        self.assert_overlay_refused(
            "'Readme.md' and existing 'README.md' differ only by case"
        )

    def test_member_below_existing_case_alias(self) -> None:
        (self.target / "Bin").mkdir(parents=True)
        self.files_download(["bin/run.sh"])
        self.assert_overlay_refused("'bin' and existing 'Bin' differ only by case")

    def test_symlink_target_case_alias_of_existing_directory(self) -> None:
        (self.target / "Real").mkdir(parents=True)
        self.link_download({"link": "real/x"})
        self.assert_overlay_refused(
            "'link': symlink target 'real' and existing 'Real' differ only by case"
        )

    def test_symlink_chain_through_ignorable_alias(self) -> None:
        # Case-insensitive HFS+ ignores U+200D, so 'jump' would reach 'Jump\u200d'.
        self.target.mkdir()
        (self.target / "ext").symlink_to(self.subdirectory("outside"))
        joined = "Jump\u200d"
        self.link_download({joined: ".", "link": "jump/ext/secret"})
        self.assert_overlay_refused(
            f"'link': symlink target 'jump' and {joined!r} differ only by case"
        )

    def test_members_differing_by_ignorable_code_point(self) -> None:
        self.target.mkdir()
        joined = "a\u200db"
        self.files_download(["ab", joined])
        self.assert_overlay_refused(f"{joined!r} and 'ab' differ only by case")

    def test_member_below_existing_ignorable_alias(self) -> None:
        (self.target / "bin").mkdir(parents=True)
        hyphenated = "b\u00adin"
        self.files_download([f"{hyphenated}/run.sh"])
        self.assert_overlay_refused(f"{hyphenated!r} and existing 'bin' differ")

    def test_failure_partway_writes_nothing(self) -> None:
        self.target.mkdir()
        _ = (self.target / "third").write_text("mine\n")
        names = ("first", "second", "third")
        members = [regular(name, name.encode()) for name in names]
        files = [entry_for(name, name.encode()) for name in names]
        _ = self.download_of(members, files)
        self.assert_overlay_refused("would replace existing file 'third'")
        self.assertEqual(sorted(os.listdir(self.target)), ["third"])

    def test_failure_after_placing_removes_added(self) -> None:
        _ = self.packed_tree()
        self.existing_tree()
        self.assert_overlay_refused(
            "cannot write the step outputs", GITHUB_OUTPUT=str(self.base)
        )

    def test_failure_after_restricted_directories(self) -> None:
        self.directory_download()
        self.target.mkdir()
        self.assert_overlay_refused(
            "cannot write the step outputs", GITHUB_OUTPUT=str(self.base)
        )

    def test_verification_failure_removes_staging(self) -> None:
        _ = self.packed_tree()
        self.existing_tree()
        original = tarfile.TarFile.extractall

        def plant(archive: tarfile.TarFile, path: str, **kwargs: object) -> None:
            extract = cast("Callable[..., None]", original)
            extract(archive, path, **kwargs)
            _ = (Path(path) / "empty" / "planted.txt").write_text("planted\n")

        with mock.patch.object(tarfile.TarFile, "extractall", plant):
            self.assert_overlay_refused("present but not listed in the manifest")

    def hook_rename(
        self,
        before: Callable[[str, int], None] | None = None,
        after: Callable[[str, int], None] | None = None,
    ) -> AbstractContextManager[object]:
        """Patch the overlay's no-replace rename to call before and after it.

        Each hook gets the destination name and its directory descriptor.
        """
        from scripts.unpack_rename import rename_noreplace as real

        def rename(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
            if before is not None:
                before(dst, dst_dir_fd)
            real(src_dir_fd, src, dst_dir_fd, dst)
            if after is not None:
                after(dst, dst_dir_fd)

        return mock.patch("scripts.unpack_overlay.rename_noreplace", rename)

    def race(self, name: str) -> dict[str, tuple[str, int, str]]:
        """Plant a file at name just before unpack adds it; return the listing before.

        The plant happens inside the call that adds name, after every
        check, so only a call that refuses to replace can catch it.
        Asserts the step fails naming name and the planted file survives.
        """
        planted: list[str] = []

        def plant(path: str, dir_fd: int) -> None:
            if path != name or planted:
                return
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=dir_fd)
            _ = os.write(fd, b"planted\n")
            os.close(fd)
            planted.append(path)

        self.target.mkdir()
        before = listing(self.workspace)
        with self.hook_rename(before=plant):
            self.assertEqual(self.run_overlay(), 1, self.stdout)
        self.assertEqual(planted, [name])
        self.assertIn(f"{name}: appeared in the destination", self.stdout)
        self.assertEqual((self.target / name).read_bytes(), b"planted\n")
        return before

    def assert_race_refused(self, name: str) -> None:
        """Assert a path planted at name survives and nothing else is left."""
        before = self.race(name)
        planted_path = f"checkout/{name}"
        after = listing(self.workspace)
        self.assertEqual(after[planted_path][0], "file")
        del after[planted_path]
        self.assertEqual(after, before)

    def test_file_appearing_before_added(self) -> None:
        _ = self.packed_tree()
        self.assert_race_refused("shared.txt")

    def test_symlink_appearing_before_added(self) -> None:
        _ = self.packed_tree()
        self.assert_race_refused("link")

    def test_directory_appearing_before_added(self) -> None:
        _ = self.packed_tree()
        self.assert_race_refused("org")

    def fail_after_placing(
        self,
        after: Callable[[str, int], None],
        message: str = "cannot write the step outputs",
    ) -> None:
        """Run overlay, calling after on each path added, then fail the step.

        Asserts the step fails naming message.
        """
        with self.hook_rename(after=after):
            self.assertEqual(
                self.run_overlay(GITHUB_OUTPUT=str(self.base)), 1, self.stdout
            )
        self.assertIn(message, self.stdout)

    def test_file_appearing_inside_new_directory(self) -> None:
        _ = self.packed_tree()
        self.target.mkdir()
        before = listing(self.workspace)

        def plant(path: str, _dir_fd: int) -> None:
            if path == "org":
                _ = (self.target / "org" / "pkg" / "theirs").write_text("theirs\n")

        self.fail_after_placing(plant)
        after = listing(self.workspace)
        for path in ("checkout/org", "checkout/org/pkg"):
            self.assertEqual(after.pop(path)[0], "dir", path)
        self.assertEqual(after.pop("checkout/org/pkg/theirs")[0], "file")
        self.assertEqual(after, before)
        self.assertIn("org/pkg: Directory not empty", self.stdout)

    def assert_replacement_kept(
        self,
        name: str,
        replace: Callable[[Path], None],
        message: str = "cannot write the step outputs",
    ) -> None:
        """Assert rollback keeps what replaces name right after unpack adds it."""
        _ = self.packed_tree()
        self.target.mkdir()
        before = listing(self.workspace)
        replaced: list[os.stat_result] = []

        def swap(path: str, _dir_fd: int) -> None:
            if path == name:
                replace(self.target / name)
                replaced.append((self.target / name).lstat())

        self.fail_after_placing(swap, message)
        self.assertIn(f"{name} in place: another process replaced it", self.stdout)
        kept = (self.target / name).lstat()
        self.assertEqual(kept.st_ino, replaced[0].st_ino)
        after = listing(self.workspace)
        _ = after.pop(f"checkout/{name}")
        self.assertEqual(after, before)

    def test_file_replaced_after_added(self) -> None:
        def replace(path: Path) -> None:
            spare = path.with_name("spare")
            _ = spare.write_text("theirs\n")
            os.replace(spare, path)

        self.assert_replacement_kept("shared.txt", replace)
        self.assertEqual((self.target / "shared.txt").read_text(), "theirs\n")

    def test_directory_replaced_after_added(self) -> None:
        def replace(path: Path) -> None:
            # Keep the original, so its inode cannot be reused.
            _ = path.rename(self.base / "original")
            path.mkdir()

        # Its mode would be set next, so the step fails there.
        self.assert_replacement_kept(
            "empty", replace, "empty: replaced in the destination"
        )
        self.assertTrue((self.target / "empty").is_dir())

    def test_symlink_recreated_after_added(self) -> None:
        # Nothing keeps the original here: its inode would be free for reuse.
        original: list[int] = []

        def replace(path: Path) -> None:
            original.append(path.lstat().st_ino)
            path.unlink()
            path.symlink_to("theirs")

        self.assert_replacement_kept("link", replace)
        self.assertEqual(os.readlink(self.target / "link"), "theirs")
        self.assertNotEqual((self.target / "link").lstat().st_ino, original[0])

    def test_directory_recreated_after_added(self) -> None:
        original: list[int] = []

        def replace(path: Path) -> None:
            original.append(path.lstat().st_ino)
            path.rmdir()
            path.mkdir()

        self.assert_replacement_kept(
            "empty", replace, "empty: replaced in the destination"
        )
        self.assertNotEqual((self.target / "empty").lstat().st_ino, original[0])

    def test_too_many_directories_for_open_file_limit(self) -> None:
        self.directory_download()
        self.target.mkdir()
        with (
            mock.patch("resource.getrlimit", return_value=(64, 64)),
            mock.patch("resource.setrlimit", side_effect=ValueError),
        ):
            self.assert_overlay_refused("directories needs")

    def assert_mode_kept(
        self, name: str, replace: Callable[[Path], None], keep: bool = True
    ) -> None:
        """Assert the archive's mode never reaches what replaces name once added.

        With keep, the original moves aside, so its inode cannot be
        reused; without, it is unlinked.
        """
        self.target.mkdir()
        before = listing(self.workspace)

        def swap(path: str, _dir_fd: int) -> None:
            if path == name:
                if keep:
                    _ = (self.target / name).rename(self.base / "original")
                else:
                    (self.target / name).unlink()
                replace(self.target / name)

        with self.hook_rename(after=swap):
            self.assertEqual(self.run_overlay(), 1, self.stdout)
        self.assertIn(f"{name}: replaced in the destination", self.stdout)
        after = listing(self.workspace)
        self.assertEqual(stat.S_IMODE((self.target / name).lstat().st_mode), 0o700)
        _ = after.pop(f"checkout/{name}")
        self.assertEqual(after, before)

    def test_directory_replaced_before_mode_set(self) -> None:
        self.directory_download()
        self.assert_mode_kept("d", lambda path: path.mkdir(mode=0o700))

    def test_file_replaced_before_owner_read_dropped(self) -> None:
        data = b"data\n"
        _ = self.download_of([regular("f", data, 0o200)], [entry_for("f", data, 0o200)])

        def replace(path: Path) -> None:
            _ = path.write_text("theirs\n")
            path.chmod(0o700)

        self.assert_mode_kept("f", replace)
        self.assertEqual((self.target / "f").read_text(), "theirs\n")

    def test_file_recreated_before_owner_read_dropped(self) -> None:
        data = b"data\n"
        _ = self.download_of([regular("f", data, 0o200)], [entry_for("f", data, 0o200)])

        def replace(path: Path) -> None:
            _ = path.write_text("theirs\n")
            path.chmod(0o700)

        self.assert_mode_kept("f", replace, keep=False)
        self.assertEqual((self.target / "f").read_text(), "theirs\n")

    def test_rollback_keeps_replaced_entry(self) -> None:
        from scripts.unpack_rollback import Added, remove_added

        self.target.mkdir()
        _ = (self.target / "x").write_text("theirs\n")
        info = (self.target / "x").lstat()
        entry = Added(str(self.target), "x", info.st_dev, info.st_ino + 1, False)
        problems = remove_added([entry])
        self.assertEqual(len(problems), 1)
        self.assertIn("another process replaced it", problems[0])
        self.assertEqual((self.target / "x").read_text(), "theirs\n")
        self.assertEqual(os.listdir(self.target), ["x"])

    def test_rollback_keeps_entry_replaced_before_removal(self) -> None:
        from scripts.unpack_rollback import Added, remove_added

        self.target.mkdir()
        _ = (self.target / "x").write_text("ours\n")
        info = (self.target / "x").lstat()
        entry = Added(str(self.target), "x", info.st_dev, info.st_ino, False)
        real_unlink = os.unlink
        swapped: list[bool] = []

        def unlink(path: str, *, dir_fd: int | None = None) -> None:
            if not swapped:
                swapped.append(True)
                spare = self.base / "spare"
                _ = spare.write_text("theirs\n")
                os.replace(spare, self.target / "x")
            real_unlink(path, dir_fd=dir_fd)

        with mock.patch("os.unlink", unlink):
            self.assertEqual(remove_added([entry]), [])
        self.assertEqual(swapped, [True])
        self.assertEqual((self.target / "x").read_text(), "theirs\n")
        self.assertEqual(os.listdir(self.target), ["x"])

    def test_failure_after_unreadable_directory_with_file(self) -> None:
        data = b"data\n"
        members = [special("c", tarfile.DIRTYPE), special("c/d", tarfile.DIRTYPE)]
        members[0][0].mode = 0o000
        members[1][0].mode = 0o300
        members.append(regular("c/d/f", data))
        _ = self.download_of(members, [entry_for("c/d/f", data)])
        self.target.mkdir()
        self.assert_overlay_refused(
            "cannot write the step outputs", GITHUB_OUTPUT=str(self.base)
        )
        self.assertEqual(os.listdir(self.target), [])

    def test_missing_destination(self) -> None:
        _ = self.packed_tree()
        self.assert_overlay_refused("must already exist")
        self.assertFalse(os.path.lexists(self.target))

    def test_symlinked_destination(self) -> None:
        _ = self.packed_tree()
        outside = self.subdirectory("outside")
        self.target.symlink_to(outside)
        self.assert_overlay_refused("'checkout' is a symlink")
        self.assertEqual(list(outside.iterdir()), [])

    def test_default_refuses_existing_destination(self) -> None:
        _ = self.packed_tree()
        self.existing_tree()
        before = listing(self.workspace)
        self.assertEqual(self.run_unpack(INPUT_OVERLAY="false"), 1)
        self.assertIn("must not exist", self.stdout)
        self.assertEqual(listing(self.workspace), before)

    def test_non_boolean(self) -> None:
        _ = self.packed_tree()
        self.existing_tree()
        for value in ("True", "yes", "1", ""):
            with self.subTest(value=value):
                self.assert_overlay_refused(
                    "overlay: must be exactly 'true' or 'false'", INPUT_OVERLAY=value
                )

    def nested_destination(self) -> tuple[Path, Path, dict[str, tuple[str, int, str]]]:
        """Make parent/checkout and an outside tree; return both and its listing.

        The outside tree holds an empty checkout of its own, where a
        write that followed a swapped parent would land.
        """
        (self.workspace / "parent" / "checkout").mkdir(parents=True)
        outside = self.subdirectory("outside")
        (outside / "checkout").mkdir()
        return self.workspace / "moved" / "checkout", outside, listing(outside)

    def swap_parent(self, outside: Path) -> None:
        """Move parent to moved and put a symlink to outside in its place."""
        _ = (self.workspace / "parent").rename(self.workspace / "moved")
        (self.workspace / "parent").symlink_to(outside)

    def test_parent_swapped_after_checks(self) -> None:
        _ = self.packed_tree()
        moved, outside, before = self.nested_destination()
        from scripts.unpack_overlay import scan_overlay as real

        def scan(*args: object) -> object:
            self.swap_parent(outside)
            return cast("Callable[..., object]", real)(*args)

        with mock.patch("scripts.unpack.scan_overlay", scan):
            code = self.run_overlay(INPUT_DESTINATION="parent/checkout")
        self.assertEqual(code, 0, self.stdout)
        self.assertEqual(listing(outside), before)
        self.assertEqual((moved / "org" / "pkg" / "a.pom").read_text(), "<project/>\n")

    def test_parent_swapped_with_absolute_link(self) -> None:
        moved, outside, before = self.nested_destination()
        checkout = self.workspace / "parent" / "checkout"
        (checkout / "real").mkdir()
        (checkout / "abs").symlink_to(checkout.resolve() / "real")
        self.link_download({"link": "abs"})
        from scripts.unpack_overlay import scan_overlay as real

        def scan(*args: object) -> object:
            self.swap_parent(outside)
            return cast("Callable[..., object]", real)(*args)

        with mock.patch("scripts.unpack.scan_overlay", scan):
            code = self.run_overlay(INPUT_DESTINATION="parent/checkout")
        self.assertEqual(code, 1, self.stdout)
        self.assertIn("'link': symlink target resolves outside", self.stdout)
        self.assertEqual(listing(outside), before)
        self.assertEqual(sorted(os.listdir(moved)), ["abs", "real"])

    def test_parent_swapped_before_rollback(self) -> None:
        _ = self.packed_tree()
        moved, outside, before = self.nested_destination()
        swapped: list[str] = []

        def swap(path: str, _dir_fd: int) -> None:
            if not swapped:
                self.swap_parent(outside)
                swapped.append(path)

        with self.hook_rename(after=swap):
            code = self.run_overlay(
                INPUT_DESTINATION="parent/checkout", GITHUB_OUTPUT=str(self.base)
            )
        self.assertEqual(code, 1, self.stdout)
        self.assertIn("cannot write the step outputs", self.stdout)
        self.assertEqual(listing(outside), before)
        self.assertEqual(os.listdir(moved), [])

    def test_many_read_only_directories(self) -> None:
        data = b"data\n"
        members: list[Member] = []
        files: list[FileEntry] = []
        for index in range(OPEN_FILES_LIMIT * 2):
            info, _ = special(f"d{index}", tarfile.DIRTYPE)
            info.mode = 0o555
            members.append((info, None))
            members.append(regular(f"d{index}/f", data))
            files.append(entry_for(f"d{index}/f", data))
        _ = self.download_of(members, files)
        self.target.mkdir()
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (OPEN_FILES_LIMIT, hard))
        try:
            code = self.run_overlay()
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        self.assertEqual(code, 0, self.stdout)
        mode = stat.S_IMODE((self.target / "d0").lstat().st_mode)
        self.assertEqual(mode, 0o555)

    def replace_private(self, prefix: str, inner: str = "theirs") -> tuple[Path, Path]:
        """Move the private directory named prefix* aside; put a populated one there.

        The replacement holds inner/theirs.txt. Returns the replacement
        and the original, now in the test's base directory.
        """
        (name,) = [
            entry for entry in os.listdir(self.target) if entry.startswith(prefix)
        ]
        aside = self.base / f"aside{name}"
        _ = (self.target / name).rename(aside)
        (self.target / name / inner).mkdir(parents=True)
        _ = (self.target / name / inner / "theirs.txt").write_text("theirs\n")
        return self.target / name, aside

    def assert_replacement_survives(self, replacement: Path, inner: str) -> None:
        """Assert cleanup kept replacement, with its contents, and warned."""
        theirs = replacement / inner / "theirs.txt"
        self.assertTrue(theirs.is_file(), self.stdout)
        self.assertEqual(theirs.read_text(), "theirs\n")
        self.assertEqual(os.listdir(replacement), [inner])
        self.assertIn("another process replaced it", self.stdout)
        self.assertTrue(
            any(line.startswith("::warning::") for line in self.stdout.splitlines())
        )

    def private_replaced_after_placing(self, prefix: str) -> None:
        """Replace prefix* once the last new path is added; assert the unpack holds.

        The original, emptied through its descriptor, is all that goes:
        the replacement keeps its contents and the files added stay.
        """
        _ = self.packed_tree()
        self.existing_tree()
        found: list[tuple[Path, Path]] = []

        def swap(path: str, _dir_fd: int) -> None:
            if path == "shared.txt":
                found.append(self.replace_private(prefix))

        with self.hook_rename(after=swap):
            self.assertEqual(self.run_overlay(), 0, self.stdout)
        replacement, aside = found[0]
        self.assert_replacement_survives(replacement, "theirs")
        self.assertEqual(os.listdir(aside), [])
        self.assertTrue((self.target / "bin" / "run.sh").is_file())

    def test_pin_directory_replaced_before_release(self) -> None:
        from scripts.unpack_pins import PINS_PREFIX

        self.private_replaced_after_placing(PINS_PREFIX)

    def test_staging_replaced_before_removal(self) -> None:
        from scripts.unpack_overlay import STAGING_PREFIX

        self.private_replaced_after_placing(STAGING_PREFIX)

    def test_clear_held_through_descriptor(self) -> None:
        from scripts.unpack_rollback import remove_held

        held = self.subdirectory("held")
        outside = self.subdirectory("outside")
        _ = (outside / "keep.txt").write_text("keep\n")
        outside.chmod(0o750)
        (held / "a" / "b").mkdir(parents=True)
        _ = (held / "a" / "b" / "f").write_text("ours\n")
        (held / "link").symlink_to(outside)
        (held / "a" / "b").chmod(0o000)
        (held / "a").chmod(0o500)
        fd = os.open(held, os.O_RDONLY | os.O_DIRECTORY)
        try:
            parent = os.open(self.base, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assertTrue(remove_held(parent, "held", fd))
            finally:
                os.close(parent)
        finally:
            os.close(fd)
        self.assertFalse(os.path.lexists(held))
        self.assertEqual(os.listdir(outside), ["keep.txt"])
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o750)

    def test_quarantine_replaced_before_staging_removed(self) -> None:
        from scripts.unpack_overlay import STAGING_PREFIX
        from scripts.unpack_rollback import QUARANTINE_PREFIX

        _ = self.packed_tree()
        self.existing_tree()
        before = listing(self.workspace)
        original_extract = tarfile.TarFile.extractall
        real_rename = os.rename
        found: list[tuple[Path, Path, str]] = []

        def plant(archive: tarfile.TarFile, path: str, **kwargs: object) -> None:
            extract = cast("Callable[..., None]", original_extract)
            extract(archive, path, **kwargs)
            _ = (Path(path) / "empty" / "planted.txt").write_text("planted\n")

        def rename(src: str, dst: str, **kwargs: int | None) -> None:
            real_rename(src, dst, **kwargs)
            if not found and str(src).startswith(STAGING_PREFIX):
                # The replacement holds what the old path-based removal hit.
                replacement, aside = self.replace_private(QUARANTINE_PREFIX, dst)
                found.append((replacement, aside, dst))

        with (
            mock.patch.object(tarfile.TarFile, "extractall", plant),
            mock.patch("os.rename", rename),
        ):
            self.assertEqual(self.run_overlay(), 1, self.stdout)
        self.assertIn("present but not listed in the manifest", self.stdout)
        replacement, aside, slot = found[0]
        self.assert_replacement_survives(replacement, slot)
        self.assertEqual(os.listdir(aside), [])
        after = listing(self.workspace)
        for path in list(after):
            if path.startswith(f"checkout/{replacement.name}"):
                del after[path]
        self.assertEqual(after, before)


if __name__ == "__main__":
    _ = unittest.main()
