---
title: "Package Types"
sidebar:
  order: 4
---

APM supports three package layouts, each with distinct install semantics.
Pick the layout that matches the author's intent -- APM preserves it.

## Layout summary

| Root signal | Author intent | Install semantic |
|---|---|---|
| `.apm/` (with or without apm.yml) | "I have N independent primitives" | Hoist each primitive into the target's runtime dirs |
| `SKILL.md` (alone or with apm.yml -- HYBRID) | "I am one skill bundle" | Copy the whole bundle to `<target>/skills/<name>/` |
| `plugin.json` / `.claude-plugin/` | Claude plugin collection | Dissect via plugin artifact mapping |

## APM package (`.apm/` directory)

The classic APM layout. Primitives live under `.apm/` in typed subdirectories.
`apm install` hoists each primitive into the consumer's runtime directories
individually.

```
my-package/
+-- apm.yml
+-- .apm/
    +-- skills/
    |   +-- pr-description/SKILL.md
    +-- agents/
    |   +-- reviewer.agent.md
    +-- instructions/
        +-- team-standards.instructions.md
```

**What gets installed:** each skill, agent, and instruction is copied to its
corresponding runtime directory (e.g. `.github/skills/`, `.github/agents/`).

**When to choose:** you are shipping multiple independent primitives that
consumers may override or extend individually.

## Skill bundle (`SKILL.md` at root)

A single skill with co-located resources. The presence of `SKILL.md` at the
package root tells APM: "this entire directory is one skill -- install it as
a unit."

An optional `apm.yml` alongside `SKILL.md` makes this a **HYBRID** package.
APM still installs it as a skill bundle, but gains dependency resolution,
version metadata, and script support from the manifest.

```
code-review-skill/
+-- SKILL.md
+-- agents/
|   +-- reviewer.agent.md
+-- assets/
|   +-- checklist.md
+-- scripts/
|   +-- lint-check.sh
+-- apm.yml            # optional -- enables dependencies and scripts
```

**What gets installed:** the entire directory tree is copied to
`<target>/skills/<name>/`, preserving internal structure.

**When to choose:** you are shipping one cohesive skill that bundles its own
agents, assets, or scripts. The skill's internal layout is part of its
contract -- APM will not rearrange it.

### Metadata precedence (HYBRID packages)

When both `apm.yml` and `SKILL.md` frontmatter are present, fields are
merged with a clear precedence rule:

- **apm.yml wins** for: `name`, `version`, `license`, `dependencies`, `scripts`.
- **SKILL.md frontmatter wins** for: `description`, `allowed-tools`.
- On any shared-field conflict, apm.yml takes precedence and APM emits a
  verbose warning so the author can reconcile.

## Plugin collection (`plugin.json`)

A Claude-native plugin layout. APM dissects the plugin artifacts and maps
them into runtime directories.

```
my-plugin/
+-- plugin.json
+-- agents/
|   +-- helper.agent.md
+-- skills/
    +-- search/SKILL.md
```

**What gets installed:** each artifact listed in `plugin.json` is mapped to
the appropriate runtime directory via `_map_plugin_artifacts`.

**When to choose:** you already have a Claude plugin and want APM to
consume it without restructuring.

## See also

- [Your First Package](../../getting-started/first-package/) -- hands-on
  walkthrough for scaffolding and publishing.
- [CLI Commands](../cli-commands/) -- `apm install`, `apm pack`, and all
  options.
- [Manifest Schema](../manifest-schema/) -- full `apm.yml` field reference.
