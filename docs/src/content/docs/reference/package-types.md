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

### Metadata model (HYBRID packages)

`apm.yml` and `SKILL.md` each own their `description` field
**independently** -- APM never merges or backfills one from the other.
The two strings serve different consumers:

- `apm.yml.description` is a short human-facing tagline rendered by
  `apm view`, `apm search`, `apm deps list`, and registry/marketplace
  listings.
- `SKILL.md` `description` (frontmatter) is the agent-runtime
  invocation matcher consumed by Claude, Copilot, and other runtimes
  per the agentskills.io spec. APM copies `SKILL.md` byte-for-byte
  into `<target>/skills/<name>/` and never reads or mutates this
  field.

Other apm.yml fields (`name`, `version`, `license`, `dependencies`,
`scripts`) are owned exclusively by `apm.yml` -- there is no
SKILL.md-side equivalent and nothing to merge. `allowed-tools` lives
exclusively in `SKILL.md` frontmatter and is consumed by the agent
runtime.

When you ship a HYBRID package, populate both descriptions
independently: keep `apm.yml.description` to a short tagline (under
~80 characters) and write `SKILL.md` in whatever length and tone the
agent runtime expects. `apm pack` warns when `apm.yml.description` is
missing so the human-facing surfaces do not degrade silently while
the agent runtime keeps working.

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
