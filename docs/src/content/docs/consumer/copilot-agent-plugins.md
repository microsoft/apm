---
title: Install Agent Plugins for Copilot
description: Install a portable Agent Plugins 1.0 package and let GitHub Copilot load it live from apm_modules.
---

An [Agent Plugin](../../reference/package-types/#agent-plugin-pluginjson-with-an-agent-plugins-schema)
is a whole unit: one `plugin.json`, its `skills/`, and its root `mcp.json`.
APM installs it without taking it apart, and GitHub Copilot loads it **live**
from the bytes APM materialized.

```bash
apm install --target copilot
```

## What APM writes

Two things, both APM-owned:

| Path | Purpose |
| --- | --- |
| `apm_modules/.github/plugin/marketplace.json` | One generated catalog listing every installed Agent Plugin, each pointing at its real directory |
| `.github/copilot/settings.local.json` | Two namespaced keys: `extraKnownMarketplaces.apm` and `enabledPlugins["<plugin>@apm"]` |

```json title=".github/copilot/settings.local.json"
{
  "extraKnownMarketplaces": {
    "apm": { "source": { "source": "directory", "path": "apm_modules" } }
  },
  "enabledPlugins": { "my-plugin@apm": true }
}
```

The marketplace path is repository-relative, so the registration survives
clones, worktrees, and moved checkouts. Global installs (`apm install -g`)
write the same two keys to `~/.copilot/settings.json` with an absolute path
under `~/.apm/apm_modules`.

APM merges only those two keys. Anything else in the file is preserved
byte-for-byte in meaning; a pre-existing `apm` marketplace or `<plugin>@apm`
entry that APM does not own fails the install with an explicit message
instead of being overwritten.

## What APM does not do

- It does not copy the plugin into Copilot's private state. `copilot plugin
  list` reports a live plugin and `installed-plugins/` stays empty.
- It does not decompose `skills/` or `mcp.json` into loose primitives for the
  Copilot target -- that would load them twice.
- It does not require `--plugin-dir`, a second `copilot plugin install`, or
  any pre-seeded folder trust.
- It never edits a marketplace file you authored.

## Requirements

Native loading needs GitHub Copilot CLI **1.0.81-8 or newer**, the first
release where a directory-marketplace plugin loads live from its real
directory. On anything older, APM fails the install closed:

```
[x] Agent Plugins v1.0.0 packages need GitHub Copilot CLI >=1.0.81-8, which
    loads a directory marketplace live from its real directory; detected
    1.0.80. Older clients copy the plugin into private Copilot state, so APM
    refuses to install it there.
```

Older clients would copy the plugin into global Copilot state and leave APM
without a clean project lifecycle, so APM refuses instead of silently
degrading. The same fail-closed rule applies to non-Copilot targets.

## Lifecycle

| Command | Effect |
| --- | --- |
| `apm install` | Materializes the plugin under `apm_modules/`, then rebuilds the catalog and settings entries |
| `apm install --force` / `apm update` | Refreshes the live bytes; Copilot picks them up on `/restart` or in a new session, with no `copilot plugin update` |
| `apm deps list` | Reports which Agent Plugins APM registers natively |
| `apm uninstall` / `apm prune` | Removes only APM-owned catalog rows and settings keys, then deletes the generated catalog once it is empty |

Your plugin sources are never deleted by the registration lifecycle -- APM
only retires what it wrote.

## Troubleshooting

**Copilot does not see the plugin.** Repository-scoped settings are folder
trust gated. Start `copilot` once in the project and grant folder trust; APM
does not pre-seed trust on your behalf.

**A settings collision.** APM refuses to overwrite an `apm` marketplace entry
it does not own. Rename or remove the conflicting entry and re-run the
install.

## See also

- [Package types](../../reference/package-types/) -- how APM classifies a
  `plugin.json`.
- [`apm pack`](../../reference/cli/pack/) -- producing an Agent Plugin bundle.
- [Deploy a local bundle](../deploy-a-bundle/) -- the imperative sibling flow
  for Claude-format bundles.
