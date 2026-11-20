---
title: Release a version
description: Tag semver releases of your package, tell consumers how to pin them, and let fixes reach them as reviewed Dependabot or Renovate pull requests.
---

Use this page when your package is ready for others to depend on. A release is a
git tag. Consumers pin the tag, their lockfile records its commit, and they move
to your next release when they choose. The examples follow Ben, who maintains
`contoso/review-skills` and ships `1.1.0` after `1.0.0`.

## Tag a release

Bump `version:` in `apm.yml`, commit, then tag the same commit:

```diff title="apm.yml"
 name: review-skills
-version: 1.0.0
+version: 1.1.0
```

```bash frame="terminal"
git commit -am "Release 1.1.0"
git tag -a v1.1.0 -m "v1.1.0"
git push --follow-tags
```

APM resolves the tag; `version:` is the label consumers see in their lockfile
and in `apm deps list`, so keep the two equal.

Use [Semantic Versioning](https://semver.org/):

- **Major** (`2.0.0`): you removed or renamed a skill, agent or prompt, or
  changed what one does in a way that breaks a consumer's workflow.
- **Minor** (`1.1.0`): you added a primitive or an option.
- **Patch** (`1.0.1`): you fixed wording or a bug without changing behavior.

Never move or delete a published tag. Consumers who already installed `v1.1.0`
keep the old commit in their lockfile, new installs get the new one, and two
teams on "1.1.0" end up with different files. Ship a new patch instead.

Name tags `v1.1.0` or `1.1.0`. In a repository with several packages, prefix the
package name: `review-v1.1.0` or `review--v1.1.0`.

## What consumers write

| In `apm.yml` | Resolves to | Moves when |
|---|---|---|
| `contoso/review-skills#v1.1.0` | Exactly that tag | Dependabot, Renovate or the consumer edits the line |
| `contoso/review-skills#^1.1.0` | Highest `1.x.y` tag at or above `1.1.0` | The consumer runs `apm update` |
| `contoso/review-skills#~1.1.0` | Highest `1.1.y` tag | The consumer runs `apm update` |
| `contoso/review-skills#<40-char SHA>` | That commit | Dependabot or Renovate moves it to a newer release's commit |

In every case the lockfile records the commit, and `apm install` replays it
until something above moves it. A range picks the highest matching tag when it
is installed, records the tag and the range in the lockfile, and stays
there; it does not drift on its own. Ranges skip pre-release tags such as
`v2.0.0-rc.1` unless the range itself names a pre-release; consumers can pin the
exact tag to try one. Resolution rules:
[OpenAPM, git-semver resolution](../../specs/openapm-v01/#73-git-semver-resolution).

Recommend the exact tag. It works with Dependabot and Renovate, and every
update arrives as a pull request.

## How your release reaches consumers

When you push `v1.1.0`:

1. A consumer with Dependabot or Renovate configured for `apm` gets a pull
   request that changes `#v1.0.0` to `#v1.1.0` and regenerates `apm.lock.yaml`.
2. They review the diff: the new tag in `apm.yml` and the new commit in
   `apm.lock.yaml`, next to your changelog.
3. After merging, each clone runs `apm install` to deploy the new files.

Consumers on a range see the release in `apm outdated` and take it with
`apm update`. Setup for both bots: [Update dependencies](../../consumer/update-and-refresh/).

## Tell consumers how to install

Put the install line, with your latest tag, at the top of your README:

```bash
apm install contoso/review-skills#v1.1.0
```

Keep a `CHANGELOG.md` with one section per version. List added, changed and
removed skills, agents and prompts by name, and call out anything that runs
code, such as a new hook or MCP server: consumers whose project gates
executables must [approve it](../../consumer/approve-executables/) before it
deploys. Reviewers read your changelog next to the lockfile diff.

## Release from CI

A git tag is a complete release; consumers need nothing else. Run a release
job when you also attach built artifacts, such as a plugin bundle
([Build a plugin](../build-a-plugin/)), to the release. On GitHub Actions:

```yaml title=".github/workflows/release.yml"
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
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
        with:
          mode: release
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

`mode: release` runs the release checks, packs each package into a `.zip`
with a `.sha256` file next to it, and creates the GitHub release for the tag.
On GitLab CI, Azure Pipelines or Jenkins, install APM, run
`apm pack --archive` and upload `build/<name>-<version>.zip` with your host's
release tool. A repository that also builds a marketplace adds release checks:
[Publish to a marketplace](../publish-to-a-marketplace/#release-the-marketplace).

## Verify

```bash frame="terminal"
apm view contoso/review-skills versions
```

`v1.1.0` appears as a tag with its commit. In a consumer still on `1.0.0`,
`apm outdated` lists the newer version.

## Related

- [Update dependencies](../../consumer/update-and-refresh/): the consumer side of a release.
- [Publish to a marketplace](../publish-to-a-marketplace/): list releases so people install by name.
- [`apm view`](../../reference/cli/view/): versions and installed metadata.
