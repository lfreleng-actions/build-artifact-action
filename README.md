<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# 📦 Build Artifact

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/build-artifact-action) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![pre-commit.ci status badge]][pre-commit.ci results page] [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/build-artifact-action/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/build-artifact-action)
<!-- prettier-ignore-end -->

Packs a directory tree into a workflow artefact in one job, and unpacks
it in another.

> **Note:** packing is not implemented yet; `pack` mode fails after
> validating its inputs.

## build-artifact-action

- **pack** uploads the files under `path` (or the listed `paths`) as
  the artefact `artifact-name`.
- **unpack** downloads the artefact `artifact-name` (or `artifact-id`)
  and extracts it into `destination`.

## Unpack

The consumer treats the artefact as untrusted data: the job that
produced it ran the project's code, while the consumer may hold a
credential. `unpack` downloads the artefact of this run, never another,
into a scratch directory under `RUNNER_TEMP`. It extracts nothing until
the download holds the tar, its manifest and nothing else, the manifest names
`artifact-name` and this run's ID, hashes to `expected-digest` when
given, and matches the tar. It then checks every tar member before
extracting any. Absolute paths, `..` segments, paths nested more than
256 levels deep, devices and FIFOs, hard links to anything but an
earlier regular member or with a mode other than that member's,
symlinks whose target leaves the root or has a `..` after a named
segment, members written through an earlier symlink, and members the
manifest does not list all fail the step. Symlink targets keep their
stored spelling, `./bin/run.sh` included. It extracts with
Python's `tarfile` `data` filter, never `tar(1)`, into a `destination`
it creates itself. An existing `destination`, even an empty directory,
or a symlink above it fails the step. Regular files keep their
permission bits within `0777`: a tar member whose mode differs from the
manifest fails the step, and `unpack` drops any setuid, setgid or
sticky bit the manifest records. The extracted tree must match the
manifest file for file; on any failure after extraction began the step
removes `destination` before failing, so a later step never reads a
partial tree. Nothing in the artefact runs.

With `artifact-id`, `actions/download-artifact` reports no trusted name
for the artefact without a token, which `unpack` does not take. The
name is then informational: `unpack` compares `artifact-name` with the
name the manifest records as a consistency check, and a producer
could write any name there. What binds the content on that path is the
immutable ID the producer's upload returned, the manifest's run ID
matching this run, and `expected-digest`; pass `expected-digest` to
bind the content you unpack to the manifest the producer emitted.

## Usage Example

Pin the action to the full commit SHA of a release, never to a branch
or tag, so a workflow runs the code you reviewed. Replace the
placeholder below with that SHA and name the release in the trailing
comment.

<!-- markdownlint-disable MD046 -->

```yaml
steps:
  - name: "Pack build output"
    id: pack
    uses: lfreleng-actions/build-artifact-action@<full-commit-sha> # vX.Y.Z
    with:
      mode: "pack"
      artifact-name: "build-output"
      path: "dist"
```

<!-- markdownlint-enable MD046 -->

## Inputs

<!-- markdownlint-disable MD013 -->

| Name            | Required | Default   | Modes  | Description                                                                                                                                                         |
| --------------- | -------- | --------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| mode            | True     |           | both   | `pack` or `unpack`. Anything else fails.                                                                                                                            |
| artifact-name   | True     |           | both   | Name of the workflow artefact. Must match `^[A-Za-z0-9._-]+$`.                                                                                                      |
| artifact-id     | False    | `''`      | unpack | Numeric artefact ID from a producer's `artifact-id` output. When set, the action downloads by ID and checks `artifact-name` against the downloaded artefact's name. |
| path            | False    | `'.'`     | pack   | Directory the packed paths are relative to. `.` or relative segments of `[A-Za-z0-9._-]`, with no `..` segment. Must resolve inside `GITHUB_WORKSPACE`.             |
| paths           | False    | `''`      | pack   | Newline-separated list of files and directories under `path` to pack. Empty means everything under `path`. Each entry has the same grammar as `path`.               |
| destination     | False    |           | unpack | Directory to extract into. Same grammar as `path`. Required in `unpack` mode.                                                                                       |
| expected-digest | False    | `''`      | unpack | SHA-256 (64 lower-case hex) of the manifest. When set, the action extracts nothing unless the downloaded manifest hashes to it.                                     |
| retention-days  | False    | `'1'`     | pack   | Integer from 1 to 90, passed to the upload.                                                                                                                         |
| fail-on-empty   | False    | `'true'`  | pack   | Strict boolean. Fail when nothing matched `paths`.                                                                                                                  |
| overwrite       | False    | `'false'` | pack   | Strict boolean. Replace an artefact of the same name in this run.                                                                                                   |

<!-- markdownlint-enable MD013 -->

Strict booleans accept `true` or `false` and nothing else. An input
that belongs to the other mode fails validation unless it keeps its
default value. `path` and `destination` must resolve inside the
workspace. Every part of the path below the workspace that already
exists, the target included, must be a directory and not a symlink.
The workspace's own path may pass through a symlink.

## Outputs

<!-- markdownlint-disable MD013 -->

| Name          | Modes  | Description                                   |
| ------------- | ------ | --------------------------------------------- |
| artifact-id   | pack   | The ID `actions/upload-artifact` returned.    |
| artifact-name | both   | The name used or verified.                    |
| digest        | both   | SHA-256 of the manifest.                      |
| file-count    | both   | Files packed, or files extracted.             |
| destination   | unpack | Absolute path of the extraction root.         |

<!-- markdownlint-enable MD013 -->

## Manifest

Each packed artefact holds two files at its top level:
`build-artifact.tar`, the packed tree, and
`build-artifact.manifest.json`, the digest manifest. The `digest`
output is the SHA-256 of the manifest file's bytes. A consumer that
passes it back as `expected-digest` pins the content of the artefact,
not its name: `unpack` extracts nothing unless the manifest hashes to
that digest, the tar matches the manifest, and the extracted tree
matches the manifest file for file.

The manifest is UTF-8 JSON with sorted keys, a two-space indent and a
trailing newline, so the same content always gives the same digest:

<!-- markdownlint-disable MD013 -->

```json
{
  "action": "lfreleng-actions/build-artifact-action",
  "artifact_name": "maven-m2repo",
  "created_at": "2026-11-04T12:00:00Z",
  "files": [
    { "mode": "0755", "path": "bin/run.sh", "sha256": "...", "size": 88 },
    { "path": "link", "symlink": "bin/run.sh" },
    { "mode": "0644", "path": "org/onap/cps/cps-parent/1.0.0/cps-parent-1.0.0.pom", "sha256": "...", "size": 4021 }
  ],
  "repository": "onap/cps",
  "root": "m2repo",
  "run_attempt": "1",
  "run_id": "41830119310",
  "schema": 1,
  "sha": "32250c531...",
  "tar": { "path": "build-artifact.tar", "sha256": "...", "size": 123456 }
}
```

<!-- markdownlint-enable MD013 -->

- `files` lists every regular file and symlink under `root`, sorted by
  `path`. Paths are relative to `root`, use `/` separators, and never
  start with `./` or contain a `..` segment. The tar carries
  directories, so `files` omits them. `root`, by default the last
  component of the packed directory, follows the same path rules.
- A regular file records its `sha256`, `size` and `mode`, the octal
  permission bits from `0000` to `7777`. A symlink records its
  `symlink` target as stored, and the action never follows it.
- `run_id`, `run_attempt`, `repository` and `sha` come from
  `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_REPOSITORY` and
  `GITHUB_SHA`, so a consumer can refuse an artefact from a different
  run attempt.
- `scripts/manifest.py` writes and verifies the manifest. Verification
  fails on the first problem with an `::error::` line naming it: a
  malformed manifest, a tar whose size or SHA-256 differs, an
  extracted tree with a missing, changed or unlisted file, a listed
  file reached through a symlinked directory, or a root that is not a
  real directory.

[pre-commit.ci results page]: https://results.pre-commit.ci/latest/github/lfreleng-actions/build-artifact-action/main
[pre-commit.ci status badge]: https://results.pre-commit.ci/badge/github/lfreleng-actions/build-artifact-action/main.svg
