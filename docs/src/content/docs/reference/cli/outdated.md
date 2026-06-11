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

- **Plain tag-pinned deps** (e.g. `v1.2.3` or `1.2.3`): semver compare against the latest matching remote tag.
- **Patterned tag-pinned deps** (e.g. `my-pkg_v1.2.3`, `my-pkg--v1.2.3`, or `my-pkg-v1.2.3`): semver compare against the latest tag matching the package-specific pattern inferred from the locked ref.
- **Full-SHA revision-pinned deps**: compare the pinned SHA against the commit behind the latest annotated semver tag. Branches and lightweight tags are ignored.
- **Branch-pinned deps** (e.g. `main`): compare the locked commit SHA against the remote branch tip.
- **Default-branch deps** (no ref): compare against `main`/`master` tip.
- **Marketplace deps**: compare the installed ref against the marketplace entry's current `source.ref`.
- **Registry deps** (experimental `registries` feature): compare the lockfile's exact `version` against the highest semver on the registry that satisfies the manifest range (same resolution semantics as `apm install`). Manifest ranges come from the root `apm.yml` and from installed packages' `apm.yml` files (transitive deps). When a registry lockfile entry has no manifest range, `apm outdated` compares against the highest published version and labels the source `(lockfile)`.

Common monorepo layouts are detected automatically for `outdated` reporting. Set an explicit marketplace `tag_pattern` when your producer uses a different layout than the built-in patterns.

Local dependencies and Artifactory-hosted deps are skipped. Legacy `apm.lock` files are migrated to `apm.lock.yaml` automatically on read.

To apply the suggested updates, run `apm install --update` (see [Related](#related)).

## Options

| Option | Description |
|---|---|
| `-g, --global` | Check user-scope dependencies in `~/.apm/` instead of the current project. |
| `-v, --verbose` | For outdated tag-pinned or registry deps, also list up to 10 newer available versions/tags. |
| `-j, --parallel-checks N` | Max concurrent remote checks. Default `4`. `0` forces sequential. |

## Examples

Check project dependencies:

```bash
apm outdated
```

Sample output:

```
                        Dependency Status
  Package                       Current   Latest             Status      Source
  ----------------------------- --------- ------------------ ----------- ---------------
  acme/agent-skills             v1.2.0    v1.4.1            outdated    git tags
  acme/prompt-pack              main      9c1ab2f0          outdated    git branch
  acme/sha-pinned               a1b2c3d4  v2.0.0 (9e8d7c6b) outdated    git tags
  acme/lint-rules               v0.3.0    v0.3.0            up-to-date  git tags
  nadavy/e2e-demo               1.0.1     1.1.1            outdated    registry: corp
  microsoft/apm-review-panel    0.1.1     0.1.2            outdated    registry: corp (lockfile)
  pirate-skill@apm-marketplace  v0.2.1    v0.3.0 (...)     outdated    marketplace: apm-marketplace

  [!] 2 outdated dependencies found
```

Check user-scope deps installed under `~/.apm/`:

```bash
apm outdated --global
```

Full-SHA pins use the annotated-tag update rules described in [`apm update`](../update/).

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
| `outdated` | A newer tag, branch tip SHA, or registry version in the manifest range is available. |
| `unknown` | The remote could not be queried, or the ref could not be resolved. For registry deps, also check auth (`APM_REGISTRY_TOKEN_{NAME}`) and that the registry URL is configured. |

Registry `Source` values:

| Source pattern | Meaning |
|---|---|
| `registry: NAME` | Compared using the manifest semver range from `apm.yml` (root or an installed package). |
| `registry: NAME (lockfile)` | No manifest range found; compared against the highest published version on the registry. |

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
- [Registries guide](../../../guides/registries/) -- declare registries, publish flat archives, and consume registry-sourced deps.
