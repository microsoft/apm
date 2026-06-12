---
title: Versioning strategies
description: Lockstep, tag-pattern, and per-package versioning for marketplace producers, and how apm pack --check-versions enforces each.
sidebar:
  order: 8
---

`marketplace.versioning.strategy` in `apm.yml` controls how
`apm pack --check-versions` verifies that local-path packages agree
on their declared `version` before a release tag goes out. Pick one
of three strategies. The default is `lockstep`.

```yaml
marketplace:
  versioning:
    strategy: lockstep        # or tag_pattern, or per_package
```

The strategy only matters when your marketplace has local-path
packages (single-plugin, monorepo-hybrid). Pure aggregator
marketplaces resolve every entry against a remote git ref and skip
the local check.

## lockstep (default)

Every local package's top-level `version` must equal the
marketplace's top-level `version`. One tag, one bump, all packages
move together.

```yaml
name: acme-monorepo
version: 1.4.0
marketplace:
  versioning: { strategy: lockstep }
  packages:
    - { name: plugin-a, source: ./packages/plugin-a, version: 1.4.0 }
    - { name: plugin-b, source: ./packages/plugin-b, version: 1.4.0 }
```

`apm pack --check-versions` exits 3 if any local package's
`apm.yml` (or the inline `version:` on the marketplace entry)
disagrees with the root.

Use lockstep when:

- The packages release together on a single tag.
- Consumers expect a single coherent version across the bundle.
- You run a small monorepo where independent versions add no value.

Worked example: [`DevExpGbb/zava-agent-config`](https://github.com/DevExpGbb/zava-agent-config)
ships 7 plugins on lockstep. Its [`apm.yml`](https://github.com/DevExpGbb/zava-agent-config/blob/main/apm.yml)
omits `versioning.strategy:` (lockstep is the default), declares one
top-level `version:`, and uses `build.tagPattern: "v{version}"` so a
single repo-wide tag (`v6.1.2`) cuts all 7 tarballs together.

## tag_pattern

Each rendered tag must be unique across local packages, and every
local package must declare a `version`. Useful when you want
per-package tags (`plugin-a-v1.2.0`, `plugin-b-v0.4.1`) on a shared
release branch.

```yaml
marketplace:
  versioning: { strategy: tag_pattern }
  build:
    tagPattern: "{name}-v{version}"
  packages:
    - { name: plugin-a, source: ./packages/plugin-a, version: 1.2.0 }
    - { name: plugin-b, source: ./packages/plugin-b, version: 0.4.1 }
```

`apm pack --check-versions` exits 3 if two packages would render the
same tag, or if any local package omits `version`.

Use tag_pattern when:

- Packages release independently but share infrastructure.
- You need git tags scoped per-package for release-note tooling.
- You want a stricter check than `per_package` without forcing
  lockstep.

## per_package

The loosest gate: every local package must declare a `version`, but
versions need not equal each other or the marketplace root.

```yaml
marketplace:
  versioning: { strategy: per_package }
  packages:
    - { name: plugin-a, source: ./packages/plugin-a, version: 2.0.0 }
    - { name: plugin-b, source: ./packages/plugin-b, version: 0.1.0 }
```

`apm pack --check-versions` exits 3 only on a missing `version`.

Use per_package when:

- The monorepo is large and packages have wildly different release
  cadences.
- You manage tags out-of-band (separate release pipeline per
  package).

## Picking a strategy

| Repo shape         | Reasonable default | When to switch                                    |
|--------------------|--------------------|---------------------------------------------------|
| Single-plugin      | `lockstep`         | Never. There is only one package.                 |
| Small monorepo     | `lockstep`         | Switch to `tag_pattern` when you start cutting per-package release notes. |
| Large monorepo     | `tag_pattern`      | Switch to `per_package` when independence outweighs the uniqueness check. |
| Aggregator         | n/a                | Remote-only marketplaces are not subject to the local check. |

`--check-versions` is non-destructive. It composes with
`--check-clean` and `--dry-run`. Run it locally before tagging:

```bash
apm pack --check-versions --dry-run
```

See [Releasing from any CI](./releasing-from-any-ci/) for the full
release pipeline that runs both gates.
