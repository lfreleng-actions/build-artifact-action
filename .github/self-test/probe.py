# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Check why the action refuses a self-test case.

A composite action's '::error::' lines reach the log but not the steps
after it, so the self-test's refuse job runs the action's own scripts
here with the inputs it gives the action: validate.py, then pack.py or
unpack.py on what validation accepts, just as action.yaml chains them.
The probe passes only when a script refuses and one of its '::error::'
lines contains EXPECT_ERROR.

Reads the action's INPUT_* variables, DOWNLOAD_DIR (the artefact as
actions/download-artifact would leave it, for unpack cases) and
EXPECT_ERROR from the environment. Script output is echoed with a
prefix, so it starts no workflow command of its own.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ERROR_PREFIX = "::error::"


def read_outputs(path: str) -> dict[str, str]:
    """Parse the 'key<<delimiter' blocks a script wrote to GITHUB_OUTPUT."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    outputs: dict[str, str] = {}
    index = 0
    while index < len(lines):
        key, found, delimiter = lines[index].partition("<<")
        if not found:
            index += 1
            continue
        end = lines.index(delimiter, index + 1)
        outputs[key] = "\n".join(lines[index + 1 : end])
        index = end + 1
    return outputs


def run_script(name: str, env: Mapping[str, str]) -> tuple[int, list[str]]:
    """Run scripts/name as the action does; return its exit code and lines."""
    script = os.path.join(ROOT, "scripts", name)
    result = subprocess.run(
        [sys.executable, "-I", "-B", script],
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
    lines = (result.stdout + result.stderr).splitlines()
    print(f"{name} exited {result.returncode}:")
    for line in lines:
        print(f"  | {line}")
    return result.returncode, lines


def mode_env(
    base: Mapping[str, str], outputs: Mapping[str, str]
) -> tuple[str, dict[str, str]]:
    """Return the mode script and its environment, as action.yaml sets them."""
    env = dict(base)
    if outputs["mode"] == "pack":
        env["PACK_ROOT"] = outputs["root"]
        env["PACK_PATHS"] = outputs["paths"]
        env["PACK_ARTIFACT_NAME"] = outputs["artifact_name"]
        env["PACK_FAIL_ON_EMPTY"] = outputs["fail_on_empty"]
        return "pack.py", env
    env["INPUT_ARTIFACT_NAME"] = outputs["artifact_name"]
    env["INPUT_EXPECTED_DIGEST"] = outputs["expected_digest"]
    env["DOWNLOAD_DIR"] = base.get("DOWNLOAD_DIR", "")
    return "unpack.py", env


def probe(environ: Mapping[str, str]) -> int:
    """Run the scripts and judge their refusal; return the exit code."""
    expected = environ.get("EXPECT_ERROR", "")
    if not expected:
        print("EXPECT_ERROR: must be set")
        return 1
    scratch = tempfile.mkdtemp(prefix="self-test-probe-", dir=environ["RUNNER_TEMP"])
    env = dict(environ)
    env["GITHUB_OUTPUT"] = os.path.join(scratch, "output")
    env["GITHUB_STEP_SUMMARY"] = os.path.join(scratch, "summary")
    code, lines = run_script("validate.py", env)
    if code == 0:
        script, script_env = mode_env(env, read_outputs(env["GITHUB_OUTPUT"]))
        code, lines = run_script(script, script_env)
    if code == 0:
        print("The action accepted inputs it must refuse ❌")
        return 1
    errors = [line for line in lines if line.startswith(ERROR_PREFIX)]
    if not any(expected in line for line in errors):
        print(f"Refused, but no '::error::' line contains {expected!r} ❌")
        return 1
    print(f"Refused with the expected error {expected!r} ✅")
    return 0


if __name__ == "__main__":
    sys.exit(probe(os.environ))
