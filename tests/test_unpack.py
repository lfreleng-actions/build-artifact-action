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
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
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
        with mock.patch.object(unpack, "check_link_target"):
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


if __name__ == "__main__":
    _ = unittest.main()
