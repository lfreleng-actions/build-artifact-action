<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Adopting build-artifact-action

Each consuming lane below replaces its inline hand-off shell with the
action. A producer job packs with `mode: pack` and exports
`artifact-id` and `digest`; the consumer names the producer in
`needs:` and unpacks with `mode: unpack`, passing the producer's
`artifact-name`, `artifact-id` and `digest` (as `expected-digest`) and
a `destination` that does not yet exist. See the
[README](../README.md#usage) for the full contract.

## security-workflows: sonatype-lifecycle.yaml

- [ ] Producer: the caller's own build job, or `java-workflows`'
      `build` with `upload_build_artifacts: true`.
- [ ] Add `artifact_name`, `artifact_id` and `artifact_digest`
      inputs to the lane, passed through as `artifact-name`,
      `artifact-id` and `expected-digest`.
- [ ] Reject the artefact inputs together with a `build_type` other
      than `none`: the lane either builds or unpacks, never both.
- [ ] When a caller passes the artefact inputs, the `scan` job does
      not check out the repository; it unpacks into a dedicated
      directory, such as `destination: "iq-scan"`, and scans it.
- [ ] Pack a shape the Nexus IQ CLI reads: `reactor` (the module
      `target/` outputs), `m2repo` (the local Maven repository) or
      `.python-deps` (installed Python dependencies).

Tracking: [security-workflows#180](https://github.com/lfreleng-actions/security-workflows/issues/180).

## security-workflows: sonarqube-cloud.yaml

- [ ] Producer packs a `workspace` shape: the sources, `target/**`,
      the JaCoCo XML reports and every path in
      `coverage_report_paths`, with `path: "."` and an explicit
      `paths` list.
- [ ] The lane unpacks it and runs the scanner in **CLI analysis
      mode**, never Maven mode. `analysis_mode: maven` runs the project's POM
      plugins in the job that holds the Sonar token, which breaks the
      hand-off rule, so the lane must refuse it with the artefact
      inputs.
- [ ] Pass the producer's `artifact-id` and `digest` through as with
      `sonatype-lifecycle.yaml`.

Tracking: [security-workflows#180](https://github.com/lfreleng-actions/security-workflows/issues/180).
This lane has no issue of its own yet; its split from
security-workflows#180 follows once
[java-workflows#132](https://github.com/lfreleng-actions/java-workflows/issues/132)
provides the `workspace` shape.

## java-workflows

- [ ] Move `upload_build_artifacts` onto `mode: pack`.
- [ ] Add `build_artifact_id` and `build_artifact_digest` outputs
      beside `build_artifact_name`, from the pack step's
      `artifact-id` and `digest`.
- [ ] Add a `workspace` shape beside `reactor` and `m2repo`.
- [ ] In the stage lane, pack and unpack at each edge of
      `build → sign → stage/central → attest → record`, passing
      `expected-digest` every time.
- [ ] Have `record` fetch and store the manifest itself: `unpack`
      restores the packed tree alone and keeps the manifest in its
      private download directory. Download the artefact by ID with
      `actions/download-artifact`, pinned to a commit SHA, passing
      `artifact-ids` and a fresh directory under `RUNNER_TEMP`; check
      that the SHA-256 of `build-artifact.manifest.json` equals the
      producer's `digest`, failing otherwise; then store that file
      unchanged as the record of what the lane signed and staged.

Tracking: [java-workflows#132](https://github.com/lfreleng-actions/java-workflows/issues/132).

## docker-workflows

- [ ] In each of the three lanes that download `build_artifacts`,
      replace the download with `mode: unpack` into a staging
      directory, such as `destination: "build-artifacts"`.
- [ ] Keep the existing step that copies the staged files into the
      Docker build context, reading from the `destination` output.
- [ ] Pass the producer's `artifact-id` and `digest` when the caller
      supplies them.

Tracking: [docker-workflows#130](https://github.com/lfreleng-actions/docker-workflows/issues/130).
