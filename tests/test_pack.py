# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/pack.py, run as the action's Pack step runs it."""

from __future__ import annotations

import filecmp
import hashlib
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.manifest import load, verify_tar, verify_tree
from scripts.unpack import MAX_DEPTH, scan_members

PACK = Path(__file__).resolve().parent.parent / "scripts" / "pack.py"
UNPACK = PACK.parent / "unpack.py"
TAR_NAME = "build-artifact.tar"
MANIFEST_NAME = "build-artifact.manifest.json"
COMMIT_TIME = 1793750400


def read_outputs(path: Path) -> dict[str, str]:
    """Parse the 'key<<delimiter' blocks written to GITHUB_OUTPUT."""
    lines = path.read_text().split("\n")
    outputs: dict[str, str] = {}
    index = 0
    while index < len(lines) and lines[index]:
        key, delimiter = lines[index].split("<<", 1)
        end = lines.index(delimiter, index + 1)
        outputs[key] = "\n".join(lines[index + 1 : end])
        index = end + 1
    return outputs


class PackTestCase(unittest.TestCase):
    """Run pack.py against a throwaway workspace and runner temp."""

    _base: Path | None = None

    @property
    def base(self) -> Path:
        """Return a temporary directory for this test."""
        if self._base is None:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            self._base = Path(os.path.realpath(tmp.name))
            (self._base / "workspace").mkdir()
        return self._base

    @property
    def workspace(self) -> Path:
        """Return the GITHUB_WORKSPACE for this test."""
        return self.base / "workspace"

    def tree(self, name: str = "dist") -> Path:
        """Create the sample tree under the workspace and return it.

        It holds a 0755 script, a 0644 file, a nested directory, an
        empty directory, a relative symlink and a UTF-8 file name.
        """
        root = self.workspace / name
        (root / "bin").mkdir(parents=True)
        (root / "lib" / "nested").mkdir(parents=True)
        (root / "empty").mkdir()
        script = root / "bin" / "run.sh"
        _ = script.write_text("#!/bin/sh\necho run\n")
        script.chmod(0o755)
        data = root / "lib" / "nested" / "data.txt"
        _ = data.write_text("data\n" * 1000)
        data.chmod(0o644)
        _ = (root / "lib" / "café.txt").write_text("unicode\n")
        (root / "lib" / "nested" / "run").symlink_to("../../bin/run.sh")
        return root

    _extra: dict[str, str] | None = None

    @property
    def extra(self) -> dict[str, str]:
        """Return environment entries this test adds or overrides."""
        if self._extra is None:
            self._extra = {}
        return self._extra

    def pack(
        self,
        path: str = "dist",
        paths: str = "",
        fail_on_empty: str = "true",
        temp: str = "runner-temp",
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        """Run pack.py; return the process and the outputs it wrote.

        self.extra, when set, adds to or overrides the environment.
        """
        runner_temp = self.base / temp
        runner_temp.mkdir(exist_ok=True)
        output = runner_temp / "github-output"
        _ = output.write_text("")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GITHUB_WORKSPACE": str(self.workspace),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(runner_temp / "summary.md"),
            "GITHUB_RUN_ID": "41830119310",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_REPOSITORY": "lfreleng-actions/example",
            "PACK_ROOT": os.path.realpath(self.workspace / path),
            "PACK_PATHS": paths,
            "PACK_ARTIFACT_NAME": "dist-files",
            "PACK_FAIL_ON_EMPTY": fail_on_empty,
            **self.extra,
        }
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(PACK)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return result, read_outputs(output)

    def packed(self, **kwargs: str) -> dict[str, str]:
        """Run pack.py, assert it succeeded, and return its outputs."""
        result, outputs = self.pack(**kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return outputs

    def assert_refused(self, needle: str, **kwargs: str) -> None:
        """Assert pack.py fails with an '::error::' naming needle."""
        result, outputs = self.pack(**kwargs)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::", result.stdout)
        self.assertIn(needle, result.stdout)
        self.assertNotIn("digest", outputs)

    def members(self, tar_path: str) -> list[tarfile.TarInfo]:
        """Return the members of the tar at tar_path, in archive order."""
        with tarfile.open(tar_path) as archive:
            return archive.getmembers()


# Expected to fail until scripts/pack.py implements pack mode.
@unittest.expectedFailure
class RoundTripTest(PackTestCase):
    """A packed tree unpacks to an identical tree."""

    def unpack(self, outputs: dict[str, str]) -> Path:
        """Run unpack.py on the packed artefact as a consumer would.

        Return the tree it extracted, after checking its outputs agree
        with pack's.
        """
        output = self.base / "unpack-output"
        _ = output.write_text("")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GITHUB_WORKSPACE": str(self.workspace),
            "GITHUB_RUN_ID": "41830119310",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(self.base / "unpack-summary.md"),
            "INPUT_ARTIFACT_NAME": "dist-files",
            "INPUT_DESTINATION": "unpacked",
            "INPUT_EXPECTED_DIGEST": outputs["digest"],
            "DOWNLOAD_DIR": str(Path(outputs["tar"]).parent),
        }
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(UNPACK)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        unpacked = read_outputs(output)
        self.assertEqual(unpacked["digest"], outputs["digest"])
        self.assertEqual(unpacked["file-count"], outputs["file-count"])
        return Path(unpacked["destination"])

    def assert_same_tree(self, left: Path, right: Path) -> None:
        """Assert left and right hold the same entries, modes and links."""
        names = sorted(
            os.path.relpath(os.path.join(top, name), left)
            for top, dirs, files in os.walk(left)
            for name in dirs + files
        )
        others = sorted(
            os.path.relpath(os.path.join(top, name), right)
            for top, dirs, files in os.walk(right)
            for name in dirs + files
        )
        self.assertEqual(names, others)
        for name in names:
            mine, theirs = os.lstat(left / name), os.lstat(right / name)
            self.assertEqual(stat.S_IFMT(mine.st_mode), stat.S_IFMT(theirs.st_mode))
            if stat.S_ISLNK(mine.st_mode):
                self.assertEqual(os.readlink(left / name), os.readlink(right / name))
                continue
            self.assertEqual(stat.S_IMODE(mine.st_mode), stat.S_IMODE(theirs.st_mode))
            if stat.S_ISREG(mine.st_mode):
                self.assertTrue(filecmp.cmp(left / name, right / name, shallow=False))

    def test_round_trip(self) -> None:
        root = self.tree()
        outputs = self.packed()
        extracted = self.unpack(outputs)
        self.assertEqual(extracted, self.workspace / "unpacked")
        self.assert_same_tree(root, extracted)
        manifest = load(outputs["manifest"])
        verify_tar(manifest, outputs["tar"])
        self.assertEqual(verify_tree(manifest, str(extracted)), 4)

    def test_outputs(self) -> None:
        _ = self.tree()
        outputs = self.packed()
        self.assertEqual(outputs["file-count"], "4")
        data = Path(outputs["manifest"]).read_bytes()
        self.assertEqual(outputs["digest"], hashlib.sha256(data).hexdigest())
        manifest = load(outputs["manifest"])
        self.assertEqual(manifest["artifact_name"], "dist-files")
        self.assertEqual(manifest["root"], "dist")
        self.assertEqual(
            [entry["path"] for entry in manifest["files"]],
            ["bin/run.sh", "lib/café.txt", "lib/nested/data.txt", "lib/nested/run"],
        )
        summary = (self.base / "runner-temp" / "summary.md").read_text()
        self.assertIn("dist-files", summary)
        self.assertIn(outputs["digest"], summary)

    def test_upload_directory_holds_two_files(self) -> None:
        _ = self.tree()
        outputs = self.packed()
        upload = Path(outputs["tar"]).parent
        self.assertEqual(Path(outputs["manifest"]).parent, upload)
        self.assertEqual(sorted(os.listdir(upload)), [MANIFEST_NAME, TAR_NAME])
        self.assertTrue(upload.is_relative_to(self.base / "runner-temp"))

    def test_paths_selects_entries(self) -> None:
        _ = self.tree()
        outputs = self.packed(paths="bin\nlib/nested/data.txt\nbin/run.sh")
        names = [member.name for member in self.members(outputs["tar"])]
        self.assertEqual(names, ["bin", "bin/run.sh", "lib/nested/data.txt"])
        self.assertEqual(outputs["file-count"], "2")

    def test_paths_with_dot_segments(self) -> None:
        _ = self.tree()
        outputs = self.packed(paths="./bin/run.sh\nlib/./nested/data.txt")
        names = [member.name for member in self.members(outputs["tar"])]
        self.assertEqual(names, ["bin/run.sh", "lib/nested/data.txt"])
        manifest = load(outputs["manifest"])
        self.assertEqual(
            [entry["path"] for entry in manifest["files"]],
            ["bin/run.sh", "lib/nested/data.txt"],
        )

    def test_paths_dot_slash_is_whole_root(self) -> None:
        _ = self.tree()
        whole = self.packed(temp="whole")
        dotted = self.packed(paths="./.", temp="dotted")
        self.assertEqual(whole["digest"], dotted["digest"])


# Expected to fail until scripts/pack.py implements pack mode.
@unittest.expectedFailure
class DeterminismTest(PackTestCase):
    """The same tree always gives the same tar and manifest bytes."""

    def test_byte_identical(self) -> None:
        _ = self.tree()
        first = self.packed(temp="first")
        second = self.packed(temp="second")
        self.assertNotEqual(first["tar"], second["tar"])
        for key in ("tar", "manifest"):
            with self.subTest(file=key):
                self.assertEqual(
                    Path(first[key]).read_bytes(), Path(second[key]).read_bytes()
                )
        self.assertEqual(first["digest"], second["digest"])

    def test_normalised_members(self) -> None:
        _ = self.tree()
        outputs = self.packed()
        members = self.members(outputs["tar"])
        names = [member.name for member in members]
        self.assertEqual(names, sorted(names))
        for member in members:
            with self.subTest(member=member.name):
                self.assertEqual(member.mtime, 0)
                self.assertEqual((member.uid, member.gid), (0, 0))
                self.assertEqual((member.uname, member.gname), ("", ""))
        manifest = load(outputs["manifest"])
        self.assertEqual(manifest["created_at"], "1970-01-01T00:00:00Z")

    def test_commit_time(self) -> None:
        _ = self.tree()
        git = ["git", "-C", str(self.workspace), "-c", "user.name=T"]
        git += ["-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
        date = f"@{COMMIT_TIME} +0000"
        env = {**os.environ, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            _ = subprocess.run(git + args, check=True, env=env)
        sha = subprocess.run(
            git + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.extra["GITHUB_SHA"] = sha
        outputs = self.packed()
        for member in self.members(outputs["tar"]):
            self.assertEqual(member.mtime, COMMIT_TIME)
        manifest = load(outputs["manifest"])
        self.assertEqual(manifest["created_at"], "2026-11-04T00:00:00Z")
        self.assertEqual(manifest["sha"], sha)


# Expected to fail until scripts/pack.py implements pack mode.
@unittest.expectedFailure
class GitTest(PackTestCase):
    """.git is never packed and a persisted credential stops the run."""

    def test_git_excluded_at_depth(self) -> None:
        root = self.tree()
        for parent in (root, root / "lib" / "nested"):
            (parent / ".git").mkdir()
            _ = (parent / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        _ = (root / "bin" / ".git").write_text("gitdir: ../elsewhere\n")
        outputs = self.packed()
        names = [member.name for member in self.members(outputs["tar"])]
        self.assertFalse([name for name in names if ".git" in name.split("/")])
        self.assertEqual(outputs["file-count"], "4")

    def test_git_entry_refused(self) -> None:
        root = self.tree()
        (root / ".git").mkdir()
        _ = (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        self.assert_refused(".git", paths=".git/HEAD")

    def test_root_in_git_refused(self) -> None:
        git = self.workspace / ".git"
        (git / "refs").mkdir(parents=True)
        _ = (git / "HEAD").write_text("ref: refs/heads/main\n")
        config = "[http]\n\textraheader = AUTHORIZATION: basic eA==\n"
        _ = (git / "config").write_text(config)
        for path in (".git", ".git/refs"):
            with self.subTest(path=path):
                self.assert_refused("PACK_ROOT", path=path)

    def test_extraheader_refused(self) -> None:
        root = self.tree()
        (root / ".git").mkdir()
        config = "[http]\n\textraheader = AUTHORIZATION: basic eA==\n"
        _ = (root / ".git" / "config").write_text(config)
        self.assert_refused("persist-credentials: false")

    def test_config_without_extraheader_accepted(self) -> None:
        root = self.tree()
        (root / ".git").mkdir()
        _ = (root / ".git" / "config").write_text("[core]\n\tbare = false\n")
        self.assertEqual(self.packed()["file-count"], "4")


# Expected to fail until scripts/pack.py implements pack mode.
@unittest.expectedFailure
class RefusalTest(PackTestCase):
    """Anything a consumer could not safely unpack is refused."""

    def test_paths_entry_outside_root(self) -> None:
        root = self.tree()
        (self.workspace / "other").mkdir()
        _ = (self.workspace / "other" / "x").write_text("outside\n")
        (root / "away").symlink_to("../other")
        self.assert_refused("away", paths="away/x")

    def test_missing_paths_entry(self) -> None:
        _ = self.tree()
        self.assert_refused("absent", paths="absent")

    def test_symlink_escaping_root(self) -> None:
        root = self.tree()
        (root / "lib" / "escape").symlink_to("../../outside")
        self.assert_refused("lib/escape")

    def test_absolute_symlink(self) -> None:
        root = self.tree()
        (root / "lib" / "absolute").symlink_to(root / "bin" / "run.sh")
        self.assert_refused("lib/absolute")

    def test_chained_symlink_escaping_root(self) -> None:
        root = self.tree()
        (root / "here").symlink_to(".")
        (root / "chained").symlink_to("here/../outside")
        self.assert_refused("chained")

    def test_symlink_leaving_and_reentering_root(self) -> None:
        root = self.tree()
        _ = (root / "safe").write_text("safe\n")
        (self.workspace / "outside").symlink_to("dist/safe")
        (root / "here").symlink_to(".")
        (root / "chained").symlink_to("here/../outside")
        self.assert_refused("chained")

    def test_dotdot_after_named_segment_refused(self) -> None:
        root = self.tree()
        (root / "inner").symlink_to("lib/../bin/run.sh")
        self.assert_refused("inner")

    def test_hard_link_outside(self) -> None:
        root = self.tree()
        outside = self.workspace / "outside.txt"
        _ = outside.write_text("outside\n")
        os.link(outside, root / "lib" / "linked.txt")
        self.assert_refused("lib/linked.txt")

    def test_hard_link_inside_accepted(self) -> None:
        root = self.tree()
        os.link(root / "bin" / "run.sh", root / "bin" / "again.sh")
        outputs = self.packed()
        types = {m.name: m.type for m in self.members(outputs["tar"])}
        self.assertEqual(types["bin/again.sh"], tarfile.REGTYPE)
        self.assertEqual(outputs["file-count"], "5")

    def test_fifo(self) -> None:
        root = self.tree()
        os.mkfifo(root / "lib" / "pipe")
        self.assert_refused("lib/pipe")

    def test_root_outside_workspace(self) -> None:
        _ = self.tree()
        (self.base / "elsewhere").mkdir()
        self.assert_refused("PACK_ROOT", path="../elsewhere")

    def test_runner_temp_as_root_refused(self) -> None:
        _ = self.tree()
        self.assert_refused("is RUNNER_TEMP", temp="workspace/dist")

    def test_empty_refused(self) -> None:
        (self.workspace / "dist" / "only-dirs").mkdir(parents=True)
        self.assert_refused("fail-on-empty")

    def test_empty_accepted(self) -> None:
        (self.workspace / "dist" / "only-dirs" / "nested").mkdir(parents=True)
        outputs = self.packed(fail_on_empty="false")
        self.assertEqual(outputs["file-count"], "0")
        manifest = load(outputs["manifest"])
        self.assertEqual(manifest["files"], [])
        self.assertEqual(self.members(outputs["tar"]), [])

    def test_scratch_inside_root_excluded(self) -> None:
        _ = self.tree()
        outputs = self.packed(path=".", temp="workspace/.runner-temp")
        names = [member.name for member in self.members(outputs["tar"])]
        scratch = [name for name in names if name.endswith((TAR_NAME, MANIFEST_NAME))]
        self.assertEqual(scratch, [])
        self.assertIn("dist/bin/run.sh", names)

    def test_earlier_scratch_inside_root_excluded(self) -> None:
        _ = self.tree()
        first = self.packed(path=".", temp="workspace/.runner-temp")
        second = self.packed(path=".", temp="workspace/.runner-temp")
        self.assertNotEqual(first["tar"], second["tar"])
        self.assertTrue(Path(first["tar"]).exists())
        self.assertTrue((self.workspace / ".runner-temp" / "summary.md").exists())
        names = [member.name for member in self.members(second["tar"])]
        scratch = [name for name in names if name.endswith((TAR_NAME, MANIFEST_NAME))]
        self.assertEqual(scratch, [])
        runner = [name for name in names if name.startswith(".runner-temp")]
        self.assertEqual(runner, [])
        self.assertEqual(first["digest"], second["digest"])
        tars = [Path(outputs["tar"]).read_bytes() for outputs in (first, second)]
        self.assertEqual(tars[0], tars[1])

    def test_paths_entry_in_runner_temp_refused(self) -> None:
        _ = self.tree()
        (self.workspace / ".runner-temp").mkdir()
        _ = (self.workspace / ".runner-temp" / "notes.txt").write_text("x\n")
        self.assert_refused(
            "lies in RUNNER_TEMP",
            path=".",
            paths=".runner-temp/notes.txt",
            temp="workspace/.runner-temp",
        )

    def deep(self, levels: int) -> Path:
        """Create a chain of levels directories under dist; return the last."""
        directory = self.workspace / "dist" / "/".join(["d"] * levels)
        directory.mkdir(parents=True)
        return directory

    def test_deepest_unpackable_file_accepted(self) -> None:
        _ = (self.deep(MAX_DEPTH - 1) / "f").write_text("x\n")
        outputs = self.packed()
        _ = scan_members(self.members(outputs["tar"]))

    def test_file_too_deep_refused(self) -> None:
        _ = (self.deep(MAX_DEPTH) / "f").write_text("x\n")
        self.assert_refused(f"nested deeper than {MAX_DEPTH} levels")

    def test_directory_too_deep_refused(self) -> None:
        _ = self.deep(MAX_DEPTH + 1)
        self.assert_refused(
            f"nested deeper than {MAX_DEPTH} levels", fail_on_empty="false"
        )


if __name__ == "__main__":
    _ = unittest.main()
