---
title: apm outdated
description: Check locked dependencies for newer versions
sidebar:
  order: 8
---

Compare locked dependencies against their remotes. Read-only: does not modify `apm.yml`, the lockfile, `apm_modules/`, or deployed files. Legacy `apm.lock` files are read in place, without migration.

## Synopsis

```bash
apm outdated [OPTIONS]
```

## Description

`apm outdated` reads the lockfile and queries each authenticated upstream to
detect staleness. It does not report locally cached refs as current upstream
state:

- **Plain tag-pinned deps** (e.g. `v1.2.3` or `1.2.3`): semver compare against the latest matching remote tag.
- **Patterned tag-pinned deps** (e.g. `my-pkg_v1.2.3`, `my-pkg--v1.2.3`, or `my-pkg-v1.2.3`): semver compare against the latest tag matching the package-specific pattern inferred from the locked ref. For virtual subdirectory packages (installed via `path:` in `apm.yml`), `{name}` is derived from the final path segment, so a dep with `path: packages/my-pkg` resolves tags like `my-pkg_v1.2.3`.
- **Full-SHA revision-pinned deps**: compare the pinned SHA against the commit behind the latest annotated semver tag. Branches and lightweight tags are ignored.
- **Branch-pinned deps** (e.g. `main`): compare the locked commit SHA against the remote branch tip.
- **Default-branch deps** (no ref): compare against `main`/`master` tip.
- **Marketplace deps**: compare the installed ref against the marketplace entry's current `source.ref`.
- **Registry deps** (experimental `registries` feature): `Current` is the locked version; `Latest` is the highest published parseable semver, including prereleases under existing registry ordering and build precedence. `Wanted` is the highest version satisfying the manifest constraint, using the same matching as `apm install`. Constraints come from the root or installed packages' `apm.yml` files.

Common monorepo layouts are detected automatically for `outdated` reporting. Set an explicit marketplace `tag_pattern` when your producer uses a different layout than the built-in patterns.

Local dependencies and Artifactory-hosted deps are skipped.

[`apm update`](../update/) applies constraint-respecting updates, not outside-constraint releases.

## Options

| Option | Description |
|---|---|
| `-g, --global` | Check user-scope dependencies in `~/.apm/` instead of the current project. |
| `-v, --verbose` | List up to 10 newer Git tags or matching registry versions within the constraint; lockfile-only registry rows list newer versions. |
| `-j, --parallel-checks N` | Max concurrent remote checks. Default `4`. `0` forces sequential. |

## Examples

Check project dependencies:

```bash
apm outdated
```

### Registry reporting

`Wanted` appears only when registry comparison rows exist. Mixed Git rows show
`-` in that column; Git-only columns remain unchanged. There is no `--json`
output or package filter.

Example rows with registry `corp` configured as the default:

| Package | Current | Wanted | Latest | Status | Source |
|---|---|---|---|---|---|
| org/pkg | 1.7.0 | 1.7.0 | 1.8.0 | outdated | registry: corp (outside constraint) |
| org/lockfile-only | 1.0.0 | - | 1.1.0 | outdated | registry: corp (lockfile) |
| acme/agent-skills | v1.2.0 | - | v1.4.1 | outdated | git tags |
| acme/deploy-helpers | stable | - | - | unknown | registry (pinned ref) |

The first row uses an exact `1.7.0` or `=1.7.0` constraint. `apm update`
keeps it at `1.7.0`. To select `1.8.0` explicitly with the same configured
registry:

```bash
apm install 'org/pkg#1.8.0'
```

Alternatively, edit the existing selector in `apm.yml`, preserving its registry
declaration, then run `apm install`.

### Other checks

Check user-scope deps installed under `~/.apm/`:

```bash
apm outdated --global
```

Full-SHA pins use the annotated-tag update rules described in [`apm update`](../update/).

Show available tags for outdated packages:

```bash
apm outdated --verbose
```

### Monorepo subdirectory packages

Monorepo dependency installed via `path:`:

```yaml
# apm.yml
- git: https://github.com/org/monorepo.git
  path: packages/my-pkg
  ref: my-pkg_v1.0.0
```

`apm.lock.yaml` records the resolved commit SHA at lock time; the tag ref drives
`outdated` detection only and is not the integrity pin.

With a newer tag `my-pkg_v1.1.0` on the remote, `apm outdated` reports it as outdated.

Use 8 parallel checks for large dependency sets:

```bash
apm outdated -j 8
```

### Status values

| Status | Meaning |
|---|---|
| `up-to-date` | Locked ref matches the current state from the authoritative remote. |
| `outdated` | A newer tag, branch tip SHA, or published registry version is available, even outside the manifest constraint. |
| `unknown` | The remote cannot be queried or the ref cannot be resolved. Registry deps also remain unknown when no published version matches the constraint, even if `Latest` is available. Cached Git refs are not reported as current. |

Registry `Source` values:

| Source pattern | Meaning |
|---|---|
| `registry: NAME` | Registry check with a manifest constraint; no outside-constraint release identified. |
| `registry: NAME (outside constraint)` | Published `Latest` fails the manifest constraint; `Wanted` stays within it, or is `-` if no version matches. |
| `registry: NAME (lockfile)` | No known manifest constraint; compare against the highest published semver, with `Wanted` shown as `-`. |
| `registry (pinned ref)` | Literal non-semver selector (e.g. `main`, `stable`, `v1.4.2`); remains `unknown`, with no inferred upgrade. |
| `registry (no version selector)` | Manifest dep has no `#<version>` selector; `apm install` rejects it. |
| `registry (invalid manifest range)` | Manifest carries a malformed semver range (e.g. `^1.0` missing patch); `apm install` rejects it. |

## Exit codes

| Code | Condition |
|---|---|
| `0` | Check completed, including outdated or unknown dependencies. |
| `1` | No lockfile found in the selected scope. |

## Related

- [`apm update`](../update/) -- re-resolve outdated deps and rewrite the lockfile after confirmation.
- [`apm view`](../view/) -- inspect a single package's metadata or available versions.
- [`apm audit`](../audit/) -- security scan over installed primitives, suitable for CI gating.
- [Registries guide](../../../guides/registries/) -- declare registries, publish flat archives, and consume registry-sourced deps.
