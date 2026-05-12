---
title: apm outdated
description: Check locked dependencies for newer versions
sidebar:
  order: 8
---

Compare locked dependencies against their remotes to see what has new versions available. Read-only: this command does not modify `apm.lock.yaml` or touch `apm_modules/`.

## Synopsis

```bash
apm outdated [OPTIONS]
```

## Description

`apm outdated` reads `apm.lock.yaml` and queries each remote to detect staleness:

- **Tag-pinned deps** (e.g. `v1.2.3`): semver compare against the latest available remote tag.
- **Branch-pinned deps** (e.g. `main`): compare the locked commit SHA against the remote branch tip.
- **Default-branch deps** (no ref): compare against `main`/`master` tip.
- **Marketplace deps**: compare the installed ref against the marketplace entry's current `source.ref`.

Local dependencies and Artifactory-hosted deps are skipped. Legacy `apm.lock` files are migrated to `apm.lock.yaml` automatically on read.

To apply the suggested updates, run `apm install --update` (see [Related](#related)).

## Options

| Option | Description |
|---|---|
| `-g, --global` | Check user-scope dependencies in `~/.apm/` instead of the current project. |
| `-v, --verbose` | For outdated tag-pinned deps, also list up to 10 newer available tags. |
| `-j, --parallel-checks N` | Max concurrent remote checks. Default `4`. `0` forces sequential. |

## Examples

Check project dependencies:

```bash
apm outdated
```

Sample output:

```
                        Dependency Status
  Package                       Current   Latest        Status      Source
  ----------------------------- --------- ------------- ----------- ---------------
  acme/agent-skills             v1.2.0    v1.4.1        outdated    git tags
  acme/prompt-pack              main      9c1ab2f0      outdated    git branch
  acme/lint-rules               v0.3.0    v0.3.0        up-to-date  git tags
  pirate-skill@apm-marketplace  v0.2.1    v0.3.0 (...)  outdated    marketplace: apm-marketplace

  [!] 2 outdated dependencies found
```

Check user-scope deps installed under `~/.apm/`:

```bash
apm outdated --global
```

Show available tags for outdated packages:

```bash
apm outdated --verbose
```

Use 8 parallel checks for large dependency sets:

```bash
apm outdated -j 8
```

### Status values

| Status | Meaning |
|---|---|
| `up-to-date` | Locked ref matches the remote. |
| `outdated` | A newer tag (or branch tip SHA) is available. |
| `unknown` | The remote could not be queried, or the ref could not be resolved. |

## Exit codes

| Code | Condition |
|---|---|
| `0` | Check completed (including when outdated deps are reported). |
| `1` | No lockfile found in the selected scope. |

`apm outdated` is a reporting command. Finding outdated deps is not an error and does not change the exit code; wire `apm audit` into CI instead if you want gating.

## Related

- [`apm install`](../install/) -- pass `--update` to upgrade outdated deps and rewrite the lockfile.
- [`apm view`](../view/) -- inspect a single package's metadata or available versions.
- [`apm audit`](../audit/) -- security scan over installed primitives, suitable for CI gating.
