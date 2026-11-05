#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Print a sorted description of the tree under DIR: each regular file's
# SHA-256 and permission bits, each directory's permission bits and each
# symlink's target as stored. The self-test compares the listing of the
# packed fixture with the listing of every restored copy.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: bash tree.sh DIR" >&2
  exit 2
fi
cd "$1"
export LC_ALL=C
find . -type f -exec sha256sum {} + | sort -k2
find . -mindepth 1 \( -type f -o -type d \) -exec stat -c '%a %n' {} + |
  sort -k2
find . -type l -printf '%p -> %l\n' | sort
