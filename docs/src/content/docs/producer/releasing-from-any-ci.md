---
title: Releasing from any CI
description: The canonical release pipeline -- apm pack with release gates plus sha256 sidecars plus gh release create -- and how to wrap it in GitHub Actions, GitLab CI, Jenkins, or Azure DevOps.
sidebar:
  order: 7
---

A marketplace release is three steps the CLI gives you primitives
for: build with release gates, produce checksums, publish a tagged
release. Every CI system runs the same sequence. The wrappers below
differ only in syntax.

## The canonical sequence

This is the source of truth. Every wrapper on this page is a
shell-script translation of these lines.

```bash
set -euo pipefail
VERSION="${VERSION:?VERSION must be set, e.g. v1.2.3}"

apm pack --check-versions --check-clean --json > gate-report.json
apm pack --check-versions --strict-metadata --json > pack-report.json

for f in build/*.zip .claude-plugin/marketplace.json; do
  [ -f "$f" ] || continue
  sha256sum "$f" > "${f}.sha256"
done

gh release create "$VERSION" \
  build/*.zip \
  build/*.zip.sha256 \
  .claude-plugin/marketplace.json \
  .claude-plugin/marketplace.json.sha256 \
  --title "$VERSION" \
  --notes-file CHANGELOG.md
```

What each command does:

- `apm pack --check-versions --check-clean --json` runs the read-only
  release gates. `--check-versions` fails if per-package
  versions disagree with `marketplace.versioning.strategy`.
  `--check-clean` fails if the on-disk `marketplace.json` does not
  match what a fresh pack would produce, or if remote Claude package metadata
  could not be fetched to certify that regeneration -- see
  [Marketplace artifacts](../../reference/cli/pack/#marketplace-artifacts)
  for the failure modes. `apm pack --check-versions --strict-metadata`
  generates the release artifacts only after remote metadata is certifiable.
  `--json` writes a machine-readable summary to stdout; human logs go to stderr.
- `sha256sum` produces one sidecar per artifact. Consumers verify
  with `sha256sum -c <file>.sha256`.
- `gh release create` uploads the bundle, the marketplace artifact,
  and the sidecars under one tag. Use whichever release API your
  forge exposes; the file set is what matters.

:::caution[Upgrading an existing release pipeline?]
`--check-clean` is now always read-only. If an older pipeline relied on one
`apm pack --check-clean` call to both validate and produce artifacts, split it
into the gate and pack calls shown above. Install one pinned apm-cli version
for the job so both calls use identical generation logic.
:::

Authenticate `gh` with a token that has `contents: write` on the
repo. Substitute the equivalent verb for non-GitHub forges
(`glab release create`, `az repos`, REST upload).

## GitHub Actions

```yaml
name: release
on:
  push:
    tags: ["v*"]
jobs:
  release:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v5
      - uses: microsoft/apm-action@v1
        with:
          mode: release
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

[`microsoft/apm-action@v1`](https://github.com/microsoft/apm-action)
with `mode: release` is a convenience for the basic release flow. It does not
currently enforce `--strict-metadata` before artifact generation. Use the raw
`run:` form below when remote metadata must be certified, or when you need to
customize another step.

> **Reference deployment.** [`DevExpGbb/zava-agent-config`](https://github.com/DevExpGbb/zava-agent-config)
> runs this exact pipeline. The
> [v6.1.2 release](https://github.com/DevExpGbb/zava-agent-config/releases/tag/v6.1.2)
> attaches 7 per-plugin bundles + their `.sha256` companions +
> `marketplace-6.1.2.json` (15 assets total) via the workflow in
> [`.github/workflows/release.yml`](https://github.com/DevExpGbb/zava-agent-config/blob/main/.github/workflows/release.yml).
> APM `0.16.0` or newer is required. apm-action `v1.10.0` provides the
> read-only split flow; use the raw CLI block above when strict metadata
> certification is required.

:::caution[Migrating release workflows from `.tar.gz`?]
The examples below assume the new `.zip` default from `apm pack --archive`.
If your release job still uploads or hashes `build/*.tar.gz`, either update
those globs to `.zip` or add `--archive-format tar.gz` to preserve the previous
artifact format.
:::

```yaml
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install apm-cli
      - run: |
          apm pack --check-versions --check-clean --json > gate-report.json
          apm pack --check-versions --strict-metadata --json > pack-report.json
          for f in build/*.zip .claude-plugin/marketplace.json; do
            [ -f "$f" ] || continue
            sha256sum "$f" > "${f}.sha256"
          done
          gh release create "${GITHUB_REF_NAME}" \
            build/*.zip build/*.zip.sha256 \
            .claude-plugin/marketplace.json* \
            --title "${GITHUB_REF_NAME}" --notes-file CHANGELOG.md
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

## GitLab CI

```yaml
release:
  stage: release
  image: python:3.12
  rules:
    - if: '$CI_COMMIT_TAG =~ /^v/'
  script:
    - pip install apm-cli
    - apm pack --check-versions --check-clean --json > gate-report.json
    - apm pack --check-versions --strict-metadata --json > pack-report.json
    - |
      for f in build/*.zip .claude-plugin/marketplace.json; do
        [ -f "$f" ] || continue
        sha256sum "$f" > "${f}.sha256"
      done
    - |
      glab release create "$CI_COMMIT_TAG" \
        build/*.zip build/*.zip.sha256 \
        .claude-plugin/marketplace.json* \
        --notes-file CHANGELOG.md
```

## Jenkins

```groovy
pipeline {
  agent any
  stages {
    stage('release') {
      when { tag pattern: "v.*", comparator: "REGEXP" }
      steps {
        sh '''
          pip install apm-cli
          apm pack --check-versions --check-clean --json > gate-report.json
          apm pack --check-versions --strict-metadata --json > pack-report.json
          for f in build/*.zip .claude-plugin/marketplace.json; do
            [ -f "$f" ] || continue
            sha256sum "$f" > "${f}.sha256"
          done
          gh release create "${TAG_NAME}" \
            build/*.zip build/*.zip.sha256 \
            .claude-plugin/marketplace.json* \
            --notes-file CHANGELOG.md
        '''
      }
    }
  }
}
```

## Azure DevOps

```yaml
trigger:
  tags:
    include: [refs/tags/v*]
pool: { vmImage: ubuntu-latest }
steps:
  - task: UsePythonVersion@0
    inputs: { versionSpec: "3.12" }
  - script: pip install apm-cli
  - script: apm pack --check-versions --check-clean --json > gate-report.json
  - script: apm pack --check-versions --strict-metadata --json > pack-report.json
  - script: |
      for f in build/*.zip .claude-plugin/marketplace.json; do
        [ -f "$f" ] || continue
        sha256sum "$f" > "${f}.sha256"
      done
  - script: |
      gh release create "$(Build.SourceBranchName)" \
        build/*.zip build/*.zip.sha256 \
        .claude-plugin/marketplace.json* \
        --notes-file CHANGELOG.md
    env:
      GH_TOKEN: $(GITHUB_TOKEN)
```

## Troubleshooting the release gates

`apm pack` exit codes you will see in CI:

| Code | Gate              | Meaning and fix                                                                                  |
|------|-------------------|--------------------------------------------------------------------------------------------------|
| 0    | -                 | Pack succeeded; ship the artifacts.                                                              |
| 1    | runtime           | Build or network error. Inspect the JSON report; rerun.                                          |
| 2    | schema            | `apm.yml` is invalid. Fix the manifest before tagging.                                           |
| 3    | `--check-versions`| Per-package versions disagree with `marketplace.versioning.strategy`. See [Versioning strategies](../versioning-strategies/). |
| 4    | `--check-clean`   | Committed `marketplace.json` does not match a fresh pack, or remote Claude package metadata was unfetchable. For drift, run `apm pack` locally, commit the diff, then re-tag. For metadata unavailability, restore the remote source or CI credentials and rerun; committing a regenerated file cannot certify unavailable metadata. |
| 5    | `--strict-metadata`| Remote Claude package metadata could not be fetched, so `apm pack` refused to write. Retry with network access, or omit `--strict-metadata` when the default warning is acceptable. |

`--check-clean` is always read-only. `--check-versions` does not suppress normal
pack writes by itself; pair it with `--dry-run` or `--check-clean` for a
validation-only invocation. `--strict-metadata` certifies metadata before the
subsequent pack writes artifacts. Recover drift by running `apm pack` locally
without `--check-*`, inspecting the diff, and pushing a clean tag. For metadata
unavailability, restore the remote source or CI credentials instead;
regenerating a file cannot certify missing metadata.

:::note
`microsoft/apm-action@v1` is a thin convenience wrapper, not a new
abstraction. Anything it does, you can do in ten lines of shell.
Reach for the raw CLI when you need to compose with non-CI tooling
(release notes generators, signing pipelines, mirrors).
:::
