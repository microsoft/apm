---
title: Install Agent Plugins for Copilot
description: Install a portable Agent Plugins 1.0 package and let GitHub Copilot load it live from the committed .agents/plugins.
---

APM installs an Agent Plugin whole and GitHub Copilot loads it **live** from
the committed `.agents/plugins/<plugin>/` -- no `--plugin-dir`, and a fresh
clone needs no APM. An
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

Agent Plugins take the same native route as
[Claude plugins at project scope](../installing-from-marketplaces/#apm-managed-project-plugins-claude-and-copilot):

| Path | Owner | Purpose |
| --- | --- | --- |
| `.agents/plugins/<plugin>/` | APM (committed) | The approved plugin root, copied from `apm_modules/` |
| `.agents/plugins/.claude-plugin/marketplace.json` | APM (committed) | One catalog listing every deployed plugin |
| `.github/copilot/settings.json` | You (merge-only, committed) | APM adds `extraKnownMarketplaces["apm-p-<hash>"]` and `enabledPlugins["<plugin>@apm-p-<hash>"]` |

```json title=".github/copilot/settings.json"
{
  "extraKnownMarketplaces": {
    "apm-p-1a2b3c4d": { "source": { "source": "directory", "path": "./.agents/plugins" } }
  },
  "enabledPlugins": { "my-plugin@apm-p-1a2b3c4d": true }
}
```

`apm.lock.yaml` records each plugin as `native_plugin`, and that record is
APM's only ownership evidence: APM adds what the lock needs, removes only what
the previous lock produced, writes `true` only when a key is absent, and keeps
your entries and any explicit `false`. A marketplace with APM's name that
points elsewhere fails the install without a write. Global installs
(`apm install -g`) deploy to `~/.agents/plugins/` and write
`$COPILOT_HOME/settings.json` (unset or blank: `~/.copilot/settings.json`) with
an absolute path.

Earlier APM versions registered Agent Plugins from `apm_modules` through
`apm_modules/.github/plugin/apm-registration.json` and an `apm` marketplace in
`.github/copilot/settings.local.json`. The next `apm install`, `apm uninstall`
or `apm prune` removes exactly the entries that ledger recorded, then the
ledger and its catalog.

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
deployed root, catalog, settings entry, or loose primitive projection for it.

## Lifecycle

| Command | Effect |
| --- | --- |
| `apm install` | Materializes the plugin under `apm_modules/`, deploys it to `.agents/plugins/`, then updates the catalog and settings entries |
| `apm install --force` | Reapplies the current dependency selection while overriding supported collision and trust prompts; it does not select newer remote refs |
| `apm update` | Downloads and validates replacement package content while the current plugin remains available, then publishes it; a failed refresh keeps the prior content |
| `apm deps list` | Reports which Agent Plugins APM registers natively |
| `apm uninstall` / `apm prune` | Removes only the deployed roots and settings keys the lock recorded, then the catalog once it is empty |

Your plugin sources are never deleted by the registration lifecycle -- APM
only retires what it wrote.

## Troubleshooting

**Copilot does not see the plugin.** Start `copilot` from the repository root:
the project registration path is repository-relative, and repository-scoped
settings are folder-trust gated. Grant folder trust once; APM does not
pre-seed trust on your behalf.

**A settings collision.** APM refuses to overwrite a marketplace entry with
its `apm-p-<hash>` name that points somewhere other than `./.agents/plugins`,
in any settings file Copilot reads. Rename or remove that entry and re-run the
install.

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
