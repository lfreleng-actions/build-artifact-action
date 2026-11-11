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
it in another, so a job that runs a project's code can hand its output
to a job that holds a credential without either job doing both.

## Purpose

A workflow that builds a project and then uses a secret on the result
splits the work at a hand-off. The job that runs the project's code
never holds a credential; the job that holds a credential never runs
the code. The build job packs what it produced, and the credentialed
job unpacks it as data, checks it, and runs nothing from it.

`java-workflows`, `docker-workflows` and `security-workflows` each
re-implement this hand-off in inline shell, each with its own checks.
This action gives them one implementation to share: `pack` writes a
reproducible tar and a digest [manifest](#manifest), and `unpack`
verifies both before it extracts anything.

- **pack** uploads the files under `path` (or the listed `paths`) as
  the artefact `artifact-name`.
- **unpack** downloads the artefact `artifact-name` (or `artifact-id`)
  and extracts it into `destination`.

## Usage

Pin the action to the full commit SHA of a release, never to a branch
or tag, so a workflow runs the code you reviewed. Replace the
placeholders below with those SHAs and name the release in the
trailing comment.

The `build` job runs the project's code with read access and no
secrets, packs `target/`, and exports the artefact ID and digest. The
`scan` job holds a secret, does not check out or build anything, and
unpacks the artefact by ID, pinned to that digest.

<!-- markdownlint-disable MD013 MD046 -->

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    outputs:
      artifact-id: ${{ steps.pack.outputs.artifact-id }}
      digest: ${{ steps.pack.outputs.digest }}
    steps:
      - name: "Checkout repository"
        uses: actions/checkout@<full-commit-sha> # vX.Y.Z
        with:
          persist-credentials: false

      - name: "Build"
        run: mvn --batch-mode package

      - name: "Pack build output"
        id: pack
        uses: lfreleng-actions/build-artifact-action@<full-commit-sha> # vX.Y.Z
        with:
          mode: "pack"
          artifact-name: "build-output"
          path: "target"

  scan:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - name: "Unpack build output"
        id: unpack
        uses: lfreleng-actions/build-artifact-action@<full-commit-sha> # vX.Y.Z
        with:
          mode: "unpack"
          artifact-name: "build-output"
          artifact-id: ${{ needs.build.outputs.artifact-id }}
          expected-digest: ${{ needs.build.outputs.digest }}
          destination: "build-output"

      - name: "Scan build output"
        env:
          SCAN_TOKEN: ${{ secrets.SCAN_TOKEN }}
          SCAN_DIR: ${{ steps.unpack.outputs.destination }}
        run: example-scanner --directory "$SCAN_DIR"
```

<!-- markdownlint-enable MD013 MD046 -->

The consumer job must name the producer in `needs:`. That orders the
jobs, so the artefact exists before `unpack` runs, and lets the
consumer read the producer's `artifact-id` and `digest` outputs.
Hand-offs stay within one workflow run: `unpack` takes no run ID
and no token, and refuses a manifest that records another run.

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
| overlay         | False    | `'false'` | unpack | Strict boolean. Add the files to an existing `destination`, replacing nothing, instead of creating it; see [Overlay](#adding-files-to-an-existing-directory).       |
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

## Modes

### Pack

`pack` writes the selected entries under `path` to
`build-artifact.tar`, writes the [manifest](#manifest) beside it, and
uploads both files, and nothing else, as one artefact with
`actions/upload-artifact`. The tar keeps file modes and stores
symlinks as symlinks. Upload compression is off: the tar is the unit
of transfer, and zipping it again costs time on large trees.

- **Selection.** With `paths` empty, `pack` takes everything under
  `path`. Otherwise it takes each listed file, and each listed
  directory with everything below it. It never follows a symlink.
  Where `RUNNER_TEMP` lies under `path`, it skips the whole of
  `RUNNER_TEMP`: the runner's output and step summary files and the
  `build-artifact-*` directories `pack` writes the tar and manifest
  to, this run's and earlier runs', and a `paths` entry inside it
  fails. `path` must not be `RUNNER_TEMP` itself, which holds the
  runner's own files.
- **`.git` is never packed.** `pack` skips any `.git` file or
  directory at any depth, and no input includes it: a `path` or
  `paths` entry inside `.git` fails. It refuses to run when
  `path/.git/config` holds an `extraheader`, the credential
  `actions/checkout` leaves behind: check out with
  `persist-credentials: false` before packing a tree that holds the
  checkout.
- **Refusals.** `pack` fails, rather than ship something `unpack`
  would reject, on a `paths` entry that does not exist, resolves
  outside `path` or passes through a symlink; a symlink whose target
  is absolute, has a `..` after a named segment, or leaves `path`,
  resolved either against the link's own directory or through the
  symlinks it passes; a file hard-linked to one outside the
  selection; a socket, FIFO or device; an entry inside 256 or more
  nested directories below `path`, which `unpack` refuses; and a
  file, directory or symlink whose content, mode, type, symlink target,
  identity or timestamps change during the pack, a selected directory
  gaining or losing an entry included.
- **Empty selection.** With `fail-on-empty: true` (the default), a
  selection with no files fails. With `false`, `pack` uploads an
  empty tar, with no directory members either, and a manifest with an
  empty `files` list, and sets `file-count` to `0`:
  a consumer can tell "nothing built" from "upload missing".
- **Determinism.** Packing the same tree at the same commit gives a
  byte-identical tar in any job. The tar sorts members by path, with
  owner IDs `0`, empty owner names, modes masked to `0777`, and every
  time, as well as the manifest's `created_at`, set to the commit time
  of `GITHUB_SHA`, or to the epoch where git cannot give one. The
  manifest, and so `digest`, also records `artifact-name`, the name of
  `path` and the run's repository, `GITHUB_SHA`, run ID and attempt
  (see [Manifest](#manifest)). Jobs of one run attempt that pack the
  same tree under the same names get the same `digest`; a re-run or
  another run does not. `digest` pins one run's artefact, not the
  tree alone.
- **Size.** `pack` streams each file into the tar in chunks, hashing
  it on the way, and never holds a whole file in memory. Trees of
  hundreds of MB are acceptable; the runner's disk, which holds the
  tree and the tar at once, is the practical limit. Pack the output
  a consumer needs, not the whole workspace.
- **Retention.** `retention-days` defaults to `1`. The artefact is a
  hand-off between jobs of one run, not a release asset; raise it
  when a consumer job may run, or be re-run, later than a day after
  the producer.

The step summary lists the artefact's name, file count, total bytes
and digest.

### Unpack

`unpack` downloads the artefact of this run, never another, into a
fresh scratch directory under `RUNNER_TEMP`, outside the workspace. It
extracts nothing until the download holds the tar, its manifest and
nothing else, the manifest names `artifact-name` and this run's ID,
hashes to `expected-digest` when given, and matches the tar.

- **Destination.** By default `unpack` creates `destination` itself.
  An existing `destination`, even an empty directory, or a symlink
  above it fails the step. With `overlay: 'true'` it adds the files to
  a `destination` that already exists instead; see
  [Adding files to an existing directory](#adding-files-to-an-existing-directory).
- **Pre-scan.** It checks every tar member before extracting any, and
  refuses the whole artefact on the first member that breaks a rule
  in [Trust model](#trust-model).
- **Extraction.** It extracts with Python's `tarfile` `data` filter,
  never `tar(1)`. Symlink targets keep their stored spelling,
  `./bin/run.sh` included. Regular files keep their permission bits
  within `0777`: a tar member whose mode differs from the manifest
  fails the step, and `unpack` drops any setuid, setgid or sticky bit
  the manifest records. Directories get their tar modes back within
  `0777` too, once `unpack` has checked the tree.
- **Verification.** The extracted tree must match the manifest file
  for file.
- **Cleanup.** On any failure after extraction began, the step removes
  what it wrote before failing, so a later step never reads a partial
  tree: the whole `destination` by default, or with `overlay` the
  files and directories it added and nothing else. With `overlay` it
  first moves each entry it added into a private directory and checks
  there that it remains the entry `unpack` created. It removes a match,
  and a directory with `rmdir`, which fails on a non-empty one. It
  removes nothing it can no longer identify as its own: anything else
  goes back to its old path and stays, and the step reports what it
  left. Until the step ends, `unpack` keeps alive each entry it added, a
  file or symlink by a hard link in a private directory and a directory
  by an open handle, so that no other entry can take an added entry's
  identity, and so cleanup can still empty a directory whose tar mode
  denies its owner read. It raises its soft open-file limit to hold a
  handle for each new directory, and fails before adding anything when
  the hard limit is too low for those handles. Emptying a deep staging
  tree can still run out of descriptors later; see
  [Known limitations](#known-limitations). It empties its own private
  directories, for staging, pins and rollback, through the handles it
  holds on them, never by path. It removes an emptied one while its name
  still names it, and otherwise leaves whatever the name now names, with
  a warning.

Nothing in the artefact runs.

### Adding files to an existing directory

With `overlay: 'true'`, `unpack` adds the artefact's files to a
`destination` that already exists, such as the repository checkout
(`destination: "."`), so each file lands where a later step expects
it: `app/target/app-1.0.jar` beside `app/Dockerfile`. The default,
`'false'`, keeps the behaviour above.

- **The destination must exist.** It must be a real directory inside
  the workspace, with no symlink at or above it below the workspace.
  A missing `destination` fails the step; `unpack` creates nothing
  for it. `unpack` opens it once, walking down from the workspace
  without following a symlink, and from then on reaches it through
  that handle alone, cleanup included, rather than by its path.
- **Nothing gets replaced.** The artefact adds files and never
  changes one. A member that would land on a path that already
  exists, whether a file, a directory or a symlink, fails the step
  naming that path, as does a member below an existing file.
- **No escape through the existing tree.** `unpack` never follows a
  symlink already in `destination`: a member written through one fails
  the step, even when it points back inside `destination`. Each
  symlink member's target resolves against the tree as it will stand
  afterwards, existing and new links together, and one that would
  resolve outside `destination`, or through a chain too long to
  resolve, fails the step. So does a chain through an existing link
  with an absolute target once the path of `destination` no longer
  reaches the directory `unpack` opened without a symlink on the way.
- **No two spellings of one name.** A case-insensitive or normalising
  filesystem, such as the macOS default, treats `Readme.md` and
  `README.md`, or the composed and decomposed forms of `café`, as one
  name; case-insensitive HFS+ also ignores code points such as U+200D
  ZERO WIDTH JOINER. Two path segments are aliases when their Unicode
  NFC forms match after case folding, ignoring Unicode's default
  ignorable code points, but the strings differ. On every platform,
  `unpack` fails the step, naming both spellings, when two member
  paths or their prefixes are aliases, when a member path or prefix
  aliases an entry already in its directory, or when a symlink target
  reaches a member or an existing entry through an alias. It compares
  the entries each directory holds as spelled, never looking a name
  up, so the filesystem does not change the result. The cost: `unpack`
  refuses an artefact holding names that differ by case, Unicode
  normalisation or ignorable code points alone, within itself or
  against `destination`, which is rare for build output.
- **Existing directories keep their modes.** A directory member may
  name an existing directory, whose mode `unpack` leaves alone;
  directories it creates take their modes from the tar. After the
  move, `unpack` sets a mode on an entry it can still identify as one
  it added and on nothing else; finding anything else at that path
  fails the step and leaves that entry's mode alone.
- **All or nothing.** Every check runs against the current contents of
  `destination` before `unpack` extracts any member. `unpack` then
  extracts into a staging directory inside `destination`, verifies it
  against the manifest as it does without `overlay`, and after that
  moves each new top-level path in with the kernel's no-replace rename
  (`renameat2` on Linux, `renameatx_np` on macOS), which fails the
  step if the path exists by then rather than replace it. On any
  failure it removes the staging directory and everything it added,
  leaving `destination` as it found it, except where
  [Known limitations](#known-limitations) says otherwise. A platform
  or filesystem without that rename fails the step before it adds
  anything.
- **Every other rule still holds**: the manifest and digest checks,
  `expected-digest`, `artifact-id`, the member refusals, the `data`
  filter and the mode rules.

`destination` reports the directory as before, and `file-count`
counts the files added. Each call unpacks one artefact: to add
more than one, call the action once for each. Two artefacts that carry the
same path then collide, and the second call fails without writing
anything.

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
  permission bits from `0000` to `7777`; `pack` masks them to `0777`,
  as the tar stores them. A symlink records its
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

### Recording what a lane handled

The `digest` output pins one artefact, so a lane can record
it as the identity of what it built and passed on. The
`java-workflows` stage lane runs `build → sign → stage/central →
attest → record`, packing and unpacking at each edge. Its `record`
job stores the manifest, and so its digest, as the stage record: the
list of files, with their hashes, that the lane signed and staged, and
the run and commit that produced them.

`unpack` restores the packed tree alone; the manifest stays in its
private download directory and is not an output. A job that records
the manifest downloads the artefact by ID itself, checks that
`build-artifact.manifest.json` hashes to the producer's `digest`, and
stores that file unchanged.

## Trust model

The producer job runs the project's code, so `unpack` treats the
artefact as hostile data: a build may have written anything into it.
The consumer may hold a credential, so a malicious artefact must not
be able to write outside `destination`, run code, or pass itself off
as another artefact.

What `unpack` defends against: the artefact's contents, whatever the
producer job wrote into it, including symlinks, name aliasing,
collisions, modes and digests.

What is out of scope: any process running as the runner user while
the step runs, including one racing the unpack. Such a process can
already rewrite the action, its interpreter or `destination`, so no
check inside the action can hold against it. The job split this
action exists for is the defence: the consumer job runs nothing
from the artefact or the project. Also out of scope: a filesystem
whose name equivalence goes beyond Unicode NFC, case folding and the
default ignorable code points.

The handles `unpack` holds, its identity checks, its pins, the rollback
quarantine and the no-replace renames are hardening, not a guarantee:
they narrow the window in which a concurrent change can affect the
step, and promise nothing more.

Before extracting any member, `unpack` fails the step on absolute
paths, `..` segments and paths nested more than 256 levels deep. It
refuses devices and FIFOs, and hard links to anything but an earlier
regular member or with a mode other than that member's. It refuses
symlinks whose target leaves the root or has a `..` after a named
segment, members written through an earlier symlink, and members the
manifest does not list. With `overlay`, it also refuses a member that
would replace or reach through anything already in `destination`, and
on every platform any name differing by case, Unicode normalisation or
ignorable code points alone from another in the artefact or from an
existing entry, as
[Adding files to an existing directory](#adding-files-to-an-existing-directory)
sets out. Setuid, setgid and sticky bits never survive
extraction. An artefact from another run, or one that does not hash
to `expected-digest`, never reaches extraction, and a failed
extraction leaves no partial tree behind. With `overlay`, the
descriptor limit under [Known limitations](#known-limitations) can
leave the staging directory behind, holding directories but no files,
and the step reports it.

On the producer side, `pack` guards against leaking the checkout's
credential: it never packs `.git`, and refuses to run while
`path/.git/config` holds the `extraheader` that `actions/checkout`
leaves behind without `persist-credentials: false`.

With `artifact-id`, `actions/download-artifact` reports no trusted name
for the artefact without a token, which `unpack` does not take. The
name is then informational: `unpack` compares `artifact-name` with the
name the manifest records as a consistency check, and a producer
could write any name there. What binds the content on that path is the
immutable ID the producer's upload returned, the manifest's run ID
matching this run, and `expected-digest`; pass `expected-digest` to
bind the content you unpack to the manifest the producer emitted.

By design, the action does not offer:

- **Cross-run downloads.** `unpack` reads artefacts of the current run,
  and refuses a manifest that records another run ID.
- **A `github-token` input.** Downloads need no token within a run,
  and the action takes none, so it cannot leak one.
- **Docker image archives.** It packs a directory tree. Hand images
  over through a registry or a dedicated image workflow.
- **Pattern fan-in.** Each call packs or unpacks one named artefact;
  no glob merges two or more artefacts into one tree. Use `overlay`
  once per artefact to add each one to the same directory.

### Known limitations

- **`unpack` reaches staging by name.** With `overlay`, `unpack`
  holds the staging directory open, but still extracts into it,
  verifies it and moves entries out of it by name: `overlay_into()` in
  `scripts/unpack.py` passes the staging name to `fill()` and
  `place()`. A process that replaced that name during the step could
  redirect the extraction or the move. Such a process is out of scope
  under the model above.
  ([review thread](https://github.com/lfreleng-actions/build-artifact-action/pull/22#discussion_r4526203363))
- **Emptying staging can run out of descriptors.** `Pins.reserve()` in
  `scripts/unpack_pins.py` raises the soft open-file limit to hold one
  descriptor for each directory the overlay adds, plus a fixed
  `SPARE_DESCRIPTORS` of 64. After the move, `unpack` empties the
  staging directory with an `os.fwalk`, which holds one more
  descriptor for each level of the staging tree. That tree still holds
  every directory member that already exists in `destination`, so an
  overlay that adds a large number of directories, together with a
  deep chain of such existing directory members, can hit `EMFILE`
  after its files are in place. The step then fails with a Python
  traceback for the `OSError`. Rollback removes the files and
  directories the overlay moved into `destination`, but emptying the
  staging directory hits the same limit, so `unpack` leaves the
  `.build-artifact-staging-*` directory in `destination`, holding
  directories but no files, and reports it in an `::error::` line
  carrying the `EMFILE` message.
  ([review thread](https://github.com/lfreleng-actions/build-artifact-action/pull/22#discussion_r4526203467))

## Testing

The unit tests need nothing beyond the Python standard library. From
the repository root, run:

```bash
python3 -m unittest discover -s tests
```

`tests/test_readme.py` fails when the [Inputs](#inputs) or
[Outputs](#outputs) table drifts from `action.yaml`.
[`testing.yaml`](.github/workflows/testing.yaml) is the reference
workflow: it runs the unit tests, then packs the checkout twice to
check the outputs and that the digest is reproducible, and checks that
an invalid `mode` fails validation. It also unpacks with `overlay`
into a directory holding a file of its own, and checks that a
colliding file fails the step.

[pre-commit.ci results page]: https://results.pre-commit.ci/latest/github/lfreleng-actions/build-artifact-action/main
[pre-commit.ci status badge]: https://results.pre-commit.ci/badge/github/lfreleng-actions/build-artifact-action/main.svg
