---
title: apm marketplace
description: Register marketplaces, author manifests, and publish updates to consumer repositories.
sidebar:
  order: 20
---

Manage marketplaces -- both **consuming** them (registering a remote
marketplace so packages resolve by `package@marketplace` shorthand)
and **authoring** one (editing `apm.yml`'s `marketplace:` block,
validating it, and publishing updates to consumer repositories).

## Synopsis

```bash
# Consume
apm marketplace add SOURCE [--name N] [--ref R | --branch B] [--host FQDN]
apm marketplace list
apm marketplace browse NAME
apm marketplace update [NAME]
apm marketplace remove NAME
apm marketplace validate NAME

# Author
apm marketplace init [--force] [--name N] [--owner O]
apm marketplace migrate [--force | --dry-run]
apm marketplace check [--offline]
apm marketplace audit NAME [--strict] [-v]
apm marketplace doctor
apm marketplace outdated [--offline] [--include-prerelease]
apm marketplace publish [--targets FILE] [--dry-run] [--no-pr] [...]

# Edit packages in the authoring config
apm marketplace package add SOURCE [...]
apm marketplace package set NAME [...]
apm marketplace package remove NAME [--yes]
```

## Description

A marketplace is a git-hosted index of APM packages. Two roles
interact with this command:

- **Consumers** register marketplaces so dependencies in `apm.yml`
  can resolve by short name (`my-pkg@my-marketplace`) instead of a
  full git URL. See [`apm install`](../install/).
- **Authors** maintain a marketplace's `apm.yml` (`marketplace:`
  block) and ship updates to consumer repositories via
  `apm marketplace publish`.

The authoring config is the `marketplace:` block of `apm.yml` in the
current working directory. Legacy `marketplace.yml` files are still
read; use `apm marketplace migrate` to fold them into `apm.yml`.

## Subcommands

### `apm marketplace add`

Register a marketplace from a source reference. Accepted forms:

- `OWNER/REPO` -- GitHub shorthand (`acme/marketplace`).
- `HOST/OWNER/.../REPO` -- non-GitHub host shorthand
  (`gitlab.com/team/marketplace`).
- HTTPS git URL -- any git host, including Azure DevOps, GitLab,
  Gitea, Bitbucket Server, or a self-hosted git server. Add `#ref`
  to pin the marketplace, for example
  `https://gitlab.com/acme/marketplace.git#v1.0.0`.
- Hosted `marketplace.json` URL --
  `https://catalog.example.com/marketplace.json`.
- SSH URL -- `git@host:org/repo.git` style.
- Local filesystem path -- absolute (`/srv/marketplaces/agent-forge`),
  relative (`./local-mkt`), home-based (`~/code/marketplace`), or a
  direct `marketplace.json` file.
- `file://` URI -- `file:///srv/marketplaces/agent-forge.git`.

```bash
# GitHub shorthand
apm marketplace add my-org/awesome-agents

# GitLab via host shorthand
apm marketplace add gitlab.com/my-org/awesome-agents --host gitlab.com

# Azure DevOps (auth via ADO_APM_PAT, same as `apm install`)
apm marketplace add https://dev.azure.com/contoso/eng/_git/agent-forge \
    --name agent-forge

# Gitea / Bitbucket Server / self-hosted git, pinned with #ref
apm marketplace add https://gitea.example.com/org/repo.git#v1.0.0 --name custom

# Hosted marketplace.json URL
apm marketplace add https://catalog.example.com/marketplace.json --name catalog

# SSH
apm marketplace add git@gitea.example.com:org/repo.git --name custom

# Local filesystem (bare repo, working directory, or marketplace.json file)
apm marketplace add /srv/marketplaces/agent-forge.git --name agent-forge
apm marketplace add ./vendor/marketplace.json --name vendor

# file:// URI
apm marketplace add file:///srv/marketplaces/agent-forge.git --name agent-forge
```

| Flag | Description |
|---|---|
| `--name`, `-n` | Display name. Defaults to the repo name. |
| `--ref`, `-r` | Git ref (branch, tag, or SHA). Default: `main`. Applies only to git-backed sources. For HTTPS git URLs, a `#ref` fragment is equivalent and is stored as the ref. |
| `--branch`, `-b` | Deprecated alias for `--ref`. |
| `--host` | Git host FQDN for `OWNER/REPO` shorthand. Default: `github.com`. Ignored when `SOURCE` already carries a host (URL, hosted `marketplace.json`, or local path); a warning is shown for hosted JSON, local paths, or a conflicting embedded host. |
| `--verbose`, `-v` | Show detailed output. |

**Trust boundary.** APM forwards its authentication tokens
(`GITHUB_APM_PAT`, `GITLAB_APM_PAT`, `ADO_APM_PAT`) only when the
marketplace host is classified as GitHub, GitLab, or Azure DevOps.
Other git hosts -- generic HTTPS, SSH, self-hosted -- are fetched via
subprocess `git` through `GitCache`, and authentication falls through
to the host's local git credential helper. Hosted `marketplace.json`
URLs are public HTTPS only: APM sends no auth headers. Use a
git-backed marketplace for private catalogs. When packages are
installed from a hosted JSON URL, the lockfile records the source URL
and fetched content digest. See
[`getting-started/authentication`](../../../getting-started/authentication/).

**Azure DevOps.** ADO-hosted marketplaces fetch `marketplace.json`
through the Azure DevOps Items API first. Authentication uses
`ADO_APM_PAT`, with the same `az` bearer fallback as ADO package
dependencies. If the REST request is unavailable, forbidden, or cannot
read the repo shape, APM transparently falls back to the existing
subprocess git path. See
[`consumer/private-and-org-packages`](../../../consumer/private-and-org-packages/).

### `apm marketplace list`

List every registered marketplace with its source URL and tracked
branch.

```bash
apm marketplace list
apm marketplace list --verbose
```

### `apm marketplace browse NAME`

Show the packages exposed by a registered marketplace.

```bash
apm marketplace browse awesome-agents
```

### `apm marketplace update [NAME]`

Refresh the local cache for one marketplace, or all when `NAME` is
omitted.

```bash
apm marketplace update                 # refresh every registered marketplace
apm marketplace update awesome-agents  # refresh one
```

### `apm marketplace remove NAME`

Unregister a marketplace.

| Flag | Description |
|---|---|
| `--yes`, `-y` | Skip the confirmation prompt. |

### `apm marketplace validate NAME`

Validate the manifest of a registered marketplace against the schema.

### `apm marketplace init`

Add a `marketplace:` block to `apm.yml` in the current directory,
scaffolding `apm.yml` if it does not exist.

| Flag | Description |
|---|---|
| `--force` | Overwrite an existing `marketplace:` block. |
| `--name` | Marketplace/package name. Default: `my-marketplace`. |
| `--owner` | Owner name for the marketplace. |
| `--no-gitignore-check` | Skip the `.gitignore` staleness check. |

### `apm marketplace migrate`

Fold a legacy `marketplace.yml` into the `marketplace:` block of
`apm.yml`.

| Flag | Description |
|---|---|
| `--force`, `--yes`, `-y` | Overwrite an existing block. |
| `--dry-run` | Preview the proposed changes without writing them. |

### `apm marketplace check`

Validate the schema of the authoring config and verify that every
package entry resolves to a reachable git ref.

| Flag | Description |
|---|---|
| `--offline` | Schema and cached-ref checks only; no network. |

### `apm marketplace audit NAME`

Run after adding or updating a marketplace, or in CI, to verify no
plugin escapes marketplace pinning. Audit a registered marketplace for
plugin dependencies that bypass marketplace pinning. The command fetches each plugin's `apm.yml` at
its pinned ref and warns when `dependencies.apm` uses direct git
URLs, repo shorthands, or `{ git: ... }` entries instead of
`name@marketplace` refs.

| Flag | Description |
|---|---|
| `--strict` | Exit 1 when bypass warnings or unverifiable plugins are found. |
| `--verbose`, `-v` | Show clean plugins and skipped reasons. |

For the top-level content/integrity scan, see [`apm audit`](../audit/).

```bash
apm marketplace audit my-marketplace
apm marketplace audit my-marketplace --strict
```

### `apm marketplace outdated`

Show packages in the authoring config that have newer upstream
versions available.

| Flag | Description |
|---|---|
| `--offline` | Use cached refs only. |
| `--include-prerelease` | Consider prerelease tags. |

When remote tags use a non-default layout (for example `my-pkg_v1.0.1`), set
`tag_pattern: "{name}_v{version}"` on the package entry or under `build:` in
`apm.yml`:

```yaml
packages:
  - name: my-pkg
    source: org/monorepo
    version: "^1.0.0"
    tag_pattern: "{name}_v{version}"
```

If no tags match the configured pattern, `apm marketplace outdated` tries common
layouts (`v{version}`, `{name}_v{version}`, `{name}--v{version}`, etc.)
automatically. Set `tag_pattern` explicitly when your producer uses a different
layout.

### `apm marketplace package add SOURCE`

Add a package entry to the authoring config. `SOURCE` is a git repo
reference. Mutable refs (`HEAD`, branches) are auto-resolved to a
concrete SHA at write time.

| Flag | Description |
|---|---|
| `--name` | Package name. Default: repo name. |
| `--version` | Semver range (e.g. `>=1.0.0`). |
| `--ref` | Pin to a git ref (SHA, tag, or `HEAD`). |
| `--subdir`, `-s` | Subdirectory inside the source repo. |
| `--tag-pattern` | Tag pattern (e.g. `v{version}`). |
| `--tags` | Comma-separated tags. |
| `--include-prerelease` | Include prerelease versions. |
| `--no-verify` | Skip the remote reachability check. |

### `apm marketplace package set NAME`

Update fields on an existing package entry. Same flag set as
`package add` minus `--no-verify`; only the fields you pass are
modified.

### `apm marketplace package remove NAME`

Remove a package entry from the authoring config.

| Flag | Description |
|---|---|
| `--yes`, `-y` | Skip the confirmation prompt. |

## Options

Every subcommand accepts `--verbose` / `-v` for detailed output.
Flags listed per-subcommand above are the only command-specific
flags.

## Examples

Register an upstream marketplace and install a package from it:

```bash
apm marketplace add my-org/awesome-agents
apm install code-reviewer@awesome-agents
```

Bootstrap a new marketplace, add a package, and verify:

```bash
apm marketplace init --name my-marketplace --owner my-org
apm marketplace package add my-org/code-reviewer --version '>=1.0.0'
apm marketplace check
```

Preview a publish, then ship it as drafts:

```bash
apm marketplace publish --dry-run
apm marketplace publish --draft
```

## Related

- [`apm install`](../install/) -- consume packages from registered marketplaces.
- [`apm search`](../search/) -- top-level shortcut for `QUERY@MARKETPLACE` package search across registered marketplaces.
- [Publish to a marketplace](../../../producer/publish-to-a-marketplace/) -- end-to-end authoring guide.
- [Manifest schema](../../manifest-schema/) -- shape of the `marketplace:` block in `apm.yml`.
