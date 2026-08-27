---
title: Install Agent Plugins for Copilot
description: Install a portable Agent Plugins 1.0 package and let GitHub Copilot load it live from apm_modules.
---

APM installs an Agent Plugin whole and GitHub Copilot loads it **live** from
`apm_modules` -- no copy, no `--plugin-dir`. An
[Agent Plugin](../../reference/package-types/#agent-plugin-pluginjson-with-an-agent-plugins-schema)
is a whole unit: one `plugin.json`, its `skills/`, and its root `mcp.json`,
and APM installs it without taking it apart.

## Quickstart

Declare the packed plugin under `dependencies.apm` in your `apm.yml`:

```yaml title="apm.yml"
dependencies:
  apm:
    - ./build/my-plugin
```

Install it for the Copilot target:

```bash
apm install --target copilot
```

Then verify it loads live: start `copilot` from the repository root, grant
folder trust once, and confirm the plugin and its skill are present.

```
$ copilot
> plugin list   # my-plugin is listed as a live plugin
> skill list    # the plugin's skill is available
```

## What APM writes

APM writes two generated files it fully owns, and merges two namespaced keys
into your Copilot settings, which stays yours:

| Path | Owner | Purpose |
| --- | --- | --- |
| `apm_modules/.github/plugin/marketplace.json` | APM (generated) | One catalog listing every installed Agent Plugin, each pointing at its real directory |
| `apm_modules/.github/plugin/apm-registration.json` | APM (generated) | Ownership ledger -- the only record of which settings keys APM wrote, so uninstall retires exactly those and nothing else |
| `.github/copilot/settings.local.json` | You (merge-only) | APM adds two namespaced keys: `extraKnownMarketplaces.apm` and `enabledPlugins["<plugin>@apm"]` |

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
write the same two keys to `$COPILOT_HOME/settings.json` (unset or blank:
`~/.copilot/settings.json`) with an absolute path under `~/.apm/apm_modules`.

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
build where a directory-marketplace plugin loads live from its real
directory. Stable `1.0.81` satisfies the floor, so a normal upgrade is
enough; `1.0.81-8` through `1.0.81-14` are prerelease builds of the same
version and also qualify. See the
[GitHub Copilot CLI releases](https://github.com/github/copilot-cli/releases).
On anything older, APM fails the install closed:

```
[x] Agent Plugins v1.0.0 packages need GitHub Copilot CLI >=1.0.81-8, which
    loads a directory marketplace live from its real directory; detected
    1.0.80. Older clients copy the plugin into private Copilot state, so APM
    refuses to install it there. Upgrade the GitHub Copilot CLI to 1.0.81 or
    newer.
```

If no `copilot` binary is on `PATH` at all, the refusal names installation
rather than an upgrade:

```
[x] GitHub Copilot CLI was not found on PATH, so APM cannot register Agent
    Plugins v1.0.0 packages natively. Install the GitHub Copilot CLI (1.0.81
    or newer), then re-run 'apm install --target copilot'.
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

**Copilot does not see the plugin.** Start `copilot` from the repository root:
the project registration path is repository-relative, and repository-scoped
settings are folder-trust gated. Grant folder trust once; APM does not
pre-seed trust on your behalf.

**A settings collision.** APM refuses to overwrite an `apm` marketplace entry
or `<plugin>@apm` key it does not own -- ownership is proven by the ledger at
`apm_modules/.github/plugin/apm-registration.json`, not by guessing. Rename or
remove the conflicting entry and re-run the install. If you deleted the ledger,
APM can no longer prove it wrote those keys and treats them as a collision:
remove the stale `extraKnownMarketplaces.apm` and
`enabledPlugins["<plugin>@apm"]` entries by hand, then re-run `apm install` to
regenerate the ledger.

**The merge fails on a settings file with comments.** APM rewrites the Copilot
settings document as 2-space JSON. A settings file containing comments (JSONC)
is not valid JSON, so the merge fails closed with an explicit message instead
of discarding your comments. Convert it to plain JSON and re-run.

GitHub Copilot is the first harness to expose a machine-verifiable native
plugin lifecycle, which is why APM can register plugins with it live. APM is
vendor-neutral: a sibling registrar can be contributed for any harness that
ships an equivalent lifecycle.

## See also

- [Package types](../../reference/package-types/) -- how APM classifies a
  `plugin.json`.
- [`apm pack`](../../reference/cli/pack/) -- producing an Agent Plugin bundle.
- [Deploy a local bundle](../deploy-a-bundle/) -- the imperative sibling flow
  for Claude-format bundles.
