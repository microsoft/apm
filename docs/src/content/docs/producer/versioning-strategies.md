---
title: Versioning strategies
description: Lockstep vs per-package versions in APM monorepos, and how --check-versions enforces them.
sidebar:
  order: 5
---

Two strategies cover almost every multi-package APM repo:

- **Lockstep** -- every package ships with the same version, every
  release. One git tag (`vX.Y.Z`) covers the whole repo. Simplest
  mental model; works well when packages are tightly coupled.
- **Per-package** -- each package versions independently, tagged as
  `<package-name>-vX.Y.Z`. Lower release cost when one package
  changes far more often than the others.

## Pick lockstep when

- Packages share a runtime contract that breaks if versions drift.
- You release together by habit anyway.
- You want one CHANGELOG and one tag.

## Pick per-package when

- Packages have independent consumers who want predictable upgrade
  cycles.
- One package iterates much faster than the others.
- You want per-package release notes.

## How APM enforces alignment

Add a `tag_pattern` to each package in your marketplace block:

```yaml
marketplace:
  registry:
    packages:
      - source: ./packages/foo
        tag_pattern: 'foo-v{version}'
      - source: ./packages/bar
        tag_pattern: 'bar-v{version}'
```

Then run on every release:

```bash
apm pack --check-versions
```

The check fails if:

- A package's `version` in `apm.yml` does not match the git tag
  computed from its `tag_pattern`.
- Packages in a lockstep repo have diverging versions.

Pair with `apm pack --check-clean` to catch a forgotten
`apm pack` rerun before tagging. See
[Releasing from any CI](../releasing-from-any-ci/) for the wrapper
recipe.

## Migration

A lockstep repo can switch to per-package later by adding
`tag_pattern` to each package and cutting the next release with
distinct tags. There is no schema migration.

## Related

- [Repo shapes](../repo-shapes/)
- [Releasing from any CI](../releasing-from-any-ci/)
- [`apm pack`](../../reference/cli/pack/)
