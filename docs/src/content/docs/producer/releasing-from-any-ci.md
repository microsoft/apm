---
title: Releasing from any CI
description: One portable shell recipe to release APM packages from GitHub Actions, GitLab CI, Jenkins, or Azure DevOps.
sidebar:
  order: 4
---

APM is a CLI. Releasing an APM package from any CI system reduces to
running two commands inside a job that has a tag context and a
release-create credential. **APM does not generate CI pipelines** --
that would lock you in to one vendor. Instead, every CI runner just
calls the same shell.

## The portable recipe

```bash
# Inside the job that runs on a version tag (vX.Y.Z or <pkg>-vX.Y.Z):
apm pack --check-versions --check-clean
# Publish the artifacts under build/ to your release system of choice.
```

That is the whole producer-side contract:

- `apm pack` builds the bundle dir(s) and (for marketplace shapes)
  the `marketplace.json` index.
- `--check-versions` fails the job if any package version drifts from
  the marketplace index or its `tag_pattern`.
- `--check-clean` fails the job if regenerating `marketplace.json`
  produces a diff against the committed file (catches "forgot to
  re-run pack before tagging").

How you upload `build/` is your CI's job.

## GitHub Actions

> [!TIP]
> If you are on GitHub Actions, prefer
> [`apm-action`](https://github.com/microsoft/apm-action) with
> `mode: release`. It wraps the recipe below, attaches sha256
> sidecars, and writes a release-notes block to the Step Summary.

Minimum wrapper, without the action:

```yaml
name: release
on:
  push:
    tags: ['v*', '*-v*']
jobs:
  release:
    runs-on: ubuntu-latest
    permissions: { contents: write }
    steps:
      - uses: actions/checkout@v5
      - uses: microsoft/apm-action@v1
        with: { command: 'apm pack --check-versions --check-clean' }
      - run: gh release create ${{ github.ref_name }} build/*.tar.gz --generate-notes
        env: { GH_TOKEN: ${{ github.token }} }
```

## GitLab CI

```yaml
release:
  rules:
    - if: $CI_COMMIT_TAG
  image: ghcr.io/microsoft/apm:latest
  script:
    - apm pack --check-versions --check-clean
    - tar -czf release.tar.gz build/
  release:
    tag_name: $CI_COMMIT_TAG
    description: 'Release $CI_COMMIT_TAG'
  artifacts:
    paths: [release.tar.gz]
```

## Jenkins

```groovy
pipeline {
  agent { docker { image 'ghcr.io/microsoft/apm:latest' } }
  triggers { pollSCM('* * * * *') }
  stages {
    stage('release') {
      when { tag 'v*' }
      steps {
        sh 'apm pack --check-versions --check-clean'
        archiveArtifacts 'build/**'
      }
    }
  }
}
```

## Azure DevOps

```yaml
trigger:
  tags:
    include: ['v*', '*-v*']
pool: { vmImage: ubuntu-latest }
container: ghcr.io/microsoft/apm:latest
steps:
  - bash: apm pack --check-versions --check-clean
  - task: PublishPipelineArtifact@1
    inputs: { targetPath: build, artifactName: bundle }
```

## Same primitives, every vendor

The CLI surface is identical across runners. If you change CI
systems, only the wrapper changes -- not the release contract. This
is intentional: APM is a package manager, not a pipeline framework.

## Related

- [Repo shapes](../repo-shapes/)
- [Versioning strategies](../versioning-strategies/)
- [`apm pack`](../../reference/cli/pack/)
