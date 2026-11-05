#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Check a tree the action unpacked against what the producer packed.
# Reads DESTINATION, DIGEST and FILE_COUNT (the unpack step's outputs),
# EXPECTED_DIGEST and EXPECTED_COUNT (the producer's), and compares the
# tree with $RUNNER_TEMP/expected/expected.txt from tree.sh.
set -euo pipefail

here="$(dirname "${BASH_SOURCE[0]}")"
workspace="$(realpath "$GITHUB_WORKSPACE")"
if [ "$DESTINATION" != "$workspace/restored" ]; then
  echo "Error: destination '$DESTINATION' is not $workspace/restored ❌"
  exit 1
fi
bash "$here/tree.sh" "$DESTINATION" > "$RUNNER_TEMP/restored.txt"
if ! diff -u "$RUNNER_TEMP/expected/expected.txt" "$RUNNER_TEMP/restored.txt"
then
  echo "Error: restored hashes, modes or links differ from the fixture ❌"
  exit 1
fi
target="$(readlink "$DESTINATION/nested/plain-link")"
if [ "$target" != "../plain.txt" ]; then
  echo "Error: symlink target changed to '$target' ❌"
  exit 1
fi
if [ "$FILE_COUNT" != "$EXPECTED_COUNT" ]; then
  echo "Error: unpacked $FILE_COUNT files, packed $EXPECTED_COUNT ❌"
  exit 1
fi
if [ "$DIGEST" != "$EXPECTED_DIGEST" ]; then
  echo "Error: unpack digest $DIGEST is not $EXPECTED_DIGEST ❌"
  exit 1
fi
echo "Restored $FILE_COUNT files intact ✅"
echo "Restored $FILE_COUNT files intact ✅" >> "$GITHUB_STEP_SUMMARY"
