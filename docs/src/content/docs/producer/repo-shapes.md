---
title: Repo shapes for plugin authors
description: The four ways an APM producer repo can be laid out, and how to pick one.
sidebar:
  order: 1
---

APM does not impose one repo layout. The right shape depends on how
many plugins you publish, whether you also curate a marketplace, and
how you version. Four shapes cover every case we have seen in the
wild. **You do not pick a shape with a flag** -- shapes emerge from
which noun-verb subcommands you compose.

## The four shapes

| Shape | One sentence | Run |
|---|---|---|
| **single-plugin** | One repo, one plugin. | `apm plugin init` |
| **aggregator** | A marketplace that vendors other people's plugins. | `apm init` then `apm marketplace init` |
| **monorepo** | One repo, many plugins under `packages/<name>/`, one marketplace. | `apm init` + `apm marketplace init` + one `apm plugin init` per package dir |
| **hybrid** | A plugin repo that *also* publishes a marketplace for related plugins. | `apm plugin init` then `apm marketplace init` |

## When to use which

> [!TIP]
> Start small. Begin as a **single-plugin** repo. Promote to
> **monorepo** only when you have a second plugin that *must* ship in
> lockstep with the first.

- **single-plugin** -- default. Fastest path to a published plugin.
  One `apm.yml`, one `plugin.json`, one release.
- **aggregator** -- you maintain a curated index of plugins built by
  *other* teams. Your repo has no `plugin.json` at the root; it has a
  `marketplace:` block that pins external plugins by version.
- **monorepo** -- you publish a family of plugins that share a
  cadence (e.g. `zava-agent-configs`). The repo root holds the
  marketplace; each `packages/<name>/` holds one plugin. One CI
  pipeline tags and ships them all.
- **hybrid** -- your repo *is* a plugin and your repo *also* curates
  a marketplace of plugins that extend it. Rare but legitimate.

## What `apm pack` produces, per shape

| Shape | Artifacts |
|---|---|
| single-plugin | One bundle dir under `build/<plugin-name>/` |
| aggregator | One `marketplace.json` plus a bundle per resolved external plugin |
| monorepo | One `marketplace.json` plus one bundle per local package |
| hybrid | All of the above |

`apm pack --check-versions` (see [`apm pack`](../../reference/cli/pack/))
catches drift between local package versions and the marketplace
index in the monorepo and hybrid shapes.

## Migration paths

A repo can move between shapes without rewriting history:

- single-plugin -> hybrid: run `apm marketplace init`.
- single-plugin -> monorepo: move the plugin into
  `packages/<original-name>/` and run `apm marketplace init` at root.
- aggregator -> monorepo: add `packages/` and run
  `apm plugin init` inside each.

The CLI does not enforce a shape; it composes from the same two
noun-verb verbs (`apm plugin init` and `apm marketplace init`).

## Related

- [Publishing to a marketplace](../publish-to-a-marketplace/)
- [`apm marketplace`](../../reference/cli/marketplace/)
- [`apm pack`](../../reference/cli/pack/)
