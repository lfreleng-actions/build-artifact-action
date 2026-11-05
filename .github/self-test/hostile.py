# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Build a hostile artefact for the self-test's refusal cases.

Writes build-artifact.tar, holding one regular file under the member
name given, into a new OUT_DIR beside a manifest that passes every
schema check and vouches for that tar. Only unpack's member scan then
stands between the tar and the disk.

Usage: python3 -I -B hostile.py OUT_DIR ARTIFACT_NAME MEMBER
"""

from __future__ import annotations

import io
import os
import sys
import tarfile

# 'python3 -I' leaves the repository root off sys.path.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from scripts.manifest import build, load, write

TAR_NAME = "build-artifact.tar"
MANIFEST_NAME = "build-artifact.manifest.json"
PAYLOAD = b"written by a hostile artefact\n"


def main(argv: list[str]) -> int:
    """Write the hostile tar and its manifest; return the exit code."""
    if len(argv) != 4:
        print("usage: hostile.py OUT_DIR ARTIFACT_NAME MEMBER", file=sys.stderr)
        return 2
    out_dir, name, member = argv[1:]
    os.mkdir(out_dir)
    tar_path = os.path.join(out_dir, TAR_NAME)
    info = tarfile.TarInfo(member)
    info.size = len(PAYLOAD)
    info.mode = 0o644
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as archive:
        archive.addfile(info, io.BytesIO(PAYLOAD))
    with tarfile.open(tar_path, "r:") as archive:
        if archive.getnames() != [member]:
            print(f"tarfile did not keep the member name {member!r}", file=sys.stderr)
            return 1
    empty = os.path.join(out_dir, "empty")
    os.mkdir(empty)
    manifest = build(
        empty, [], artifact_name=name, tar_path=tar_path, root_name="hostile"
    )
    os.rmdir(empty)
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)
    digest = write(manifest, manifest_path)
    # Prove the manifest is one unpack would accept on its own terms.
    _ = load(manifest_path)
    print(f"Built hostile artefact with member {member!r}, manifest {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
