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

> **Note:** the interface below validates its inputs, but packing and
> unpacking are not implemented yet; both modes fail after validation.

## build-artifact-action

- **pack** uploads the files under `path` (or the listed `paths`) as
  the artefact `artifact-name`.
- **unpack** downloads the artefact `artifact-name` (or `artifact-id`)
  and extracts it into `destination`.

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

[pre-commit.ci results page]: https://results.pre-commit.ci/latest/github/lfreleng-actions/build-artifact-action/main
[pre-commit.ci status badge]: https://results.pre-commit.ci/badge/github/lfreleng-actions/build-artifact-action/main.svg
