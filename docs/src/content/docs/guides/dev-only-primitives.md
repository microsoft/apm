---
title: "Dev-only Primitives"
description: "Author maintainer-only skills, agents, and instructions that stay out of shipped artifacts."
sidebar:
  order: 7
---

Some primitives are useful while you author an APM package but should never reach consumers: a release-checklist skill for maintainers, a debugging agent that only makes sense in your repo, instructions that reference internal infrastructure. APM has one canonical pattern for this. Once you know it, three otherwise surprising behaviors stop being surprising.

## The pattern

```
your-package/
+-- apm.yml
+-- .apm/                          # shipped source root
|   +-- skills/
|   |   +-- public-skill/SKILL.md
|   +-- agents/
+-- dev/                           # maintainer-only, outside .apm/
    +-- skills/
        +-- release-checklist/SKILL.md
```

```yaml
# apm.yml
name: your-package
version: 1.0.0

dependencies:
  apm:
    - microsoft/apm-sample-package#v1.0.0

devDependencies:
  apm:
    - path: ./dev/skills/release-checklist
```

`apm install --dev` deploys the release-checklist skill to `.github/skills/release-checklist/` with `is_dev: true` in the lockfile. `apm pack` (plugin format, the default) excludes it. Consumers running `apm install your-org/your-package` never see it.

## Why outside `.apm/`?

APM treats `.apm/` as the publishable source root. The local-content scanner that builds plugin bundles operates on `.apm/` only, and it does NOT consult the devDependency marker when deciding what to include. If a dev-only skill sits under `.apm/skills/`, it ships -- even if the only reference to it in `apm.yml` is under `devDependencies`.

Authoring dev-only primitives anywhere outside `.apm/` (`dev/`, `internal/`, `.maintainer/` -- your choice) keeps them invisible to the scanner. Referencing them via local-path `devDependencies` keeps them installable on `apm install --dev` and tracked in the lockfile.

## Three behaviors this pattern works around

1. **The scanner does not honor the devDep marker.** It scans `.apm/` wholesale at pack time. The cure is to live outside `.apm/`.

2. **`includes:` is allow-list only.** There is no `exclude:` form. You cannot write `includes: [.apm/]` and expect a sibling `.apm/dev/` subtree to be stripped -- `includes` does not gate `.apm/` against itself, and the manifest schema has no exclude verb. See [Manifest section 3.9](../../reference/manifest-schema/#39-includes).

3. **Plain `apm install` deploys devDeps.** `apm install` (no flag) resolves and deploys both `dependencies` and `devDependencies`. `apm install --dev <pkg>` adds a new dev dependency to the manifest -- it is not a filter that excludes prod deps. There is currently no `--omit=dev` flag; the dev/prod separation kicks in at `apm pack` time, not at install time.

## When to use this pattern

- Maintainer-only release tooling (changelog drafters, version-bump helpers).
- Internal debugging agents you do not want consumers to load.
- Test-fixture skills referenced by your own CI but not by consumers.
- Anything that would embarrass you if it shipped.

## When NOT to use this pattern

- Shared dev tooling that another package consumes -- that is a regular remote `devDependencies` entry (`owner/test-helpers`), not a local path.
- A primitive a consumer might legitimately want -- keep it under `.apm/`.

## Verifying

```bash
apm install --dev                          # deploys with is_dev: true
grep is_dev apm.lock.yaml                  # confirm marker
apm pack --dry-run                         # confirm absence from bundle (plugin format, default)
ls build/your-package-1.0.0/skills/        # release-checklist must NOT appear
```

If the dev-only skill appears in the dry-run output, it is sitting under `.apm/`. Move it to `dev/` (or any non-`.apm/` path) and re-reference it via `path:` in `devDependencies`.

## See also

- [Anatomy of an APM Package](../../introduction/anatomy-of-an-apm-package/) -- why `.apm/` is the publishable source root.
- [Manifest Schema 3.9 -- `includes`](../../reference/manifest-schema/#39-includes) -- allow-list semantics, no exclude form.
- [Manifest Schema 5 -- `devDependencies`](../../reference/manifest-schema/#5-devdependencies) -- the field reference.
- [Pack & Distribute -- Plugin format](./pack-distribute/#plugin-format-vs-apm-format) -- what the scanner emits.
