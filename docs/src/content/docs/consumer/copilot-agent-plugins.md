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
| `apm_modules/.github/plugin/apm-registration.json` | APM (generated) | Primary ownership ledger for the settings keys APM manages |
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

APM preserves unrelated JSON keys and values semantically. Its stable
serialization may reformat the settings document.
The install fails closed only when a pre-existing `apm` marketplace entry
points somewhere other than APM's materialization root and the ledger does not
record APM as its owner. An entry that already matches what APM would write is
re-adopted silently, ledger or not. The `<plugin>@apm` enabled keys sit in
APM's namespace: once APM owns the `apm` marketplace it sets the ones it needs
and retires any leftover `@apm` key, so a lost ledger (`rm -rf apm_modules`)
still converges on re-install. Do not create manual `*@apm` activation keys;
APM reconciliation may retire them. Keys using another marketplace suffix are
preserved.

Only plugins that pass target and security admission participate in
registration and name-collision handling. If two admitted dependencies declare
the same plugin name, a direct dependency wins over a transitive dependency.
Two admitted claimants at the same precedence fail with an actionable collision
instead of registering either one. An admitted transitive dependency cannot
silently replace the owner recorded in APM's ledger.

## What APM does not do

- It does not copy the plugin into Copilot's private state. `copilot plugin
  list` reports a live plugin and `installed-plugins/` stays empty.
- It does not decompose `skills/` or `mcp.json` into loose primitives for the
  Copilot target -- that would load them twice.
- It does not require `--plugin-dir`, a second `copilot plugin install`, or
  any pre-seeded folder trust.
- It does not locate, execute, or version-check a Copilot binary during
  install, update, restore, uninstall, or prune.
- It never edits a marketplace file you authored.

## Requirements

APM does not require Copilot to be installed when it materializes or registers
the plugin. A canonical Agent Plugin is admitted when its effective targets
include `copilot` and its integrity, security, and executable gates pass.

Loading the generated projection is supported with stable GitHub Copilot CLI
**1.0.81 or newer**. Older clients may copy plugins into private Copilot state
outside APM ownership, so APM cannot guarantee that uninstall or prune removes
those client-created copies. Upgrade the runtime before using the registration.
See the
[GitHub Copilot CLI releases](https://github.com/github/copilot-cli/releases).
Non-Copilot targets remain outside this native registration path.
APM may still acquire and lock the dependency, but it creates no Copilot
catalog, ledger, settings entry, or loose primitive projection for it.

## Lifecycle

| Command | Effect |
| --- | --- |
| `apm install` | Materializes the plugin under `apm_modules/`, then rebuilds the catalog and settings entries |
| `apm install --force` | Reapplies the current dependency selection while overriding supported collision and trust prompts; it does not select newer remote refs |
| `apm update` | Downloads and validates replacement package content while the current plugin remains available, then publishes it; a failed refresh keeps the prior content |
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
whose directory differs from what it would write when the ledger at
`apm_modules/.github/plugin/apm-registration.json` does not record APM as its
owner. Rename or remove that entry and re-run the install. Deleting the ledger
is not a collision on its own: as long as the `apm` marketplace still matches,
the next install re-adopts it and regenerates the ledger, so
`rm -rf apm_modules && apm install` recovers with no hand-editing.

**The merge fails on invalid JSON or comments.** APM writes stable 2-space JSON,
so JSONC comments are not supported. Invalid JSON fails closed with an explicit
message and the original settings file is not overwritten. Fix the file
(convert it to plain JSON) or delete it, then re-run `apm install`.

## Why Copilot first

GitHub Copilot is the first harness for which APM qualifies a native plugin
lifecycle with a pinned real-binary release test. APM is vendor-neutral: a
sibling registrar can be contributed for any harness with an equivalent
qualified projection.

## See also

- [Package types](../../reference/package-types/) -- how APM classifies a
  `plugin.json`.
- [`apm pack`](../../reference/cli/pack/) -- producing an Agent Plugin bundle.
- [Deploy a local bundle](../deploy-a-bundle/) -- the imperative sibling flow
  for Claude-format bundles.
