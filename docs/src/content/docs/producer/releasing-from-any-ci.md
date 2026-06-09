---
title: Releasing from any CI
description: The canonical release pipeline -- apm pack with release gates plus sha256 sidecars plus gh release create -- and how to wrap it in GitHub Actions, GitLab CI, Jenkins, or Azure DevOps.
sidebar:
  order: 7
---

A marketplace release is three steps the CLI gives you primitives
for: build with release gates, produce checksums, publish a tagged
release. Every CI system runs the same three commands. The wrappers
below differ only in syntax.

## The canonical sequence

This is the source of truth. Every wrapper on this page is a
shell-script translation of these lines.

```bash
set -euo pipefail
VERSION="${VERSION:?VERSION must be set, e.g. v1.2.3}"

apm pack --check-versions --check-clean --json > pack-report.json

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

- `apm pack --check-versions --check-clean --json` runs the pack with
  the release gates enabled. `--check-versions` fails if per-package
  versions disagree with `marketplace.versioning.strategy`.
  `--check-clean` fails if the on-disk `marketplace.json` does not
  match what a fresh pack would produce. `--json` writes a
  machine-readable summary to stdout; human logs go to stderr.
- `sha256sum` produces one sidecar per artifact. Consumers verify
  with `sha256sum -c <file>.sha256`.
- `gh release create` uploads the bundle, the marketplace artifact,
  and the sidecars under one tag. Use whichever release API your
  forge exposes; the file set is what matters.

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
with `mode: release` is a convenience wrapper for the canonical
sequence above. It installs the CLI, runs `apm pack
--check-versions --check-clean --json`, generates the sidecars, and
calls `gh release create` against the pushed tag. Use it when you
want one less script to maintain; use the raw `run:` form below when
you need to customise any step.

> **Reference deployment.** [`DevExpGbb/zava-agent-config`](https://github.com/DevExpGbb/zava-agent-config)
> runs this exact pipeline. The
> [v6.1.2 release](https://github.com/DevExpGbb/zava-agent-config/releases/tag/v6.1.2)
> attaches 7 per-plugin tarballs + their `.sha256` companions +
> `marketplace-6.1.2.json` (15 assets total) via the workflow in
> [`.github/workflows/release.yml`](https://github.com/DevExpGbb/zava-agent-config/blob/main/.github/workflows/release.yml).
> APM `0.16.0` and apm-action `v1.9.1` or newer required.

```yaml
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install apm-cli
      - run: |
          apm pack --check-versions --check-clean --json > pack-report.json
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
    - apm pack --check-versions --check-clean --json > pack-report.json
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
          apm pack --check-versions --check-clean --json > pack-report.json
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
  - script: apm pack --check-versions --check-clean --json > pack-report.json
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
| 3    | `--check-versions`| Per-package versions disagree with `marketplace.versioning.strategy`. See [Versioning strategies](./versioning-strategies/). |
| 4    | `--check-clean`   | Committed `marketplace.json` does not match a fresh pack. Run `apm pack` locally, commit the diff (or `git commit --amend --no-edit` to fold into the current commit), then re-tag and push the updated tag (`git tag -f vX.Y.Z && git push --force-with-lease origin vX.Y.Z`). |

The gates never write to disk -- they only refuse to release.
Recover by running the same `apm pack` locally without `--check-*`,
inspecting the diff, and pushing a clean tag.

:::note
`microsoft/apm-action@v1` is a thin convenience wrapper, not a new
abstraction. Anything it does, you can do in ten lines of shell.
Reach for the raw CLI when you need to compose with non-CI tooling
(release notes generators, signing pipelines, mirrors).
:::
