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
apm marketplace add REPO [--name N] [--branch B] [--host FQDN]
apm marketplace list
apm marketplace browse NAME
apm marketplace update [NAME]
apm marketplace remove NAME
apm marketplace validate NAME

# Author
apm marketplace init [--force] [--name N] [--owner O]
apm marketplace migrate [--force | --dry-run]
apm marketplace check [--offline]
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

Register a marketplace from a repo reference. Accepts `OWNER/REPO`,
`HOST/OWNER/.../REPO`, or an HTTPS git URL.

```bash
apm marketplace add my-org/awesome-agents
apm marketplace add gitlab.com/my-org/awesome-agents --host gitlab.com
```

| Flag | Description |
|---|---|
| `--name`, `-n` | Display name. Defaults to the repo name. |
| `--branch`, `-b` | Branch to track. Default: `main`. |
| `--host` | Git host FQDN. Default: `github.com`. |
| `--verbose`, `-v` | Show detailed output. |

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

### `apm marketplace doctor`

Run environment diagnostics for marketplace publishing: git binary,
network reachability, auth (`gh`/PAT), and config sanity.

### `apm marketplace outdated`

Show packages in the authoring config that have newer upstream
versions available.

| Flag | Description |
|---|---|
| `--offline` | Use cached refs only. |
| `--include-prerelease` | Consider prerelease tags. |

### `apm marketplace publish`

Push marketplace updates to one or more **consumer** repositories,
optionally opening pull requests.

```bash
apm marketplace publish --dry-run
apm marketplace publish --targets ./consumer-targets.yml --draft
```

| Flag | Description |
|---|---|
| `--targets FILE` | Path to consumer-targets YAML. Default: `./consumer-targets.yml`. |
| `--dry-run` | Preview without pushing or opening PRs. |
| `--no-pr` | Push branches but skip PR creation. |
| `--draft` | Open PRs as drafts. |
| `--allow-downgrade` | Permit version downgrades. |
| `--allow-ref-change` | Permit switching ref types (e.g. tag to SHA). |
| `--parallel N` | Maximum concurrent target updates. Default: `4`. |
| `--yes`, `-y` | Skip the confirmation prompt. |

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
