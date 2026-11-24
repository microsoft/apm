---
title: Install plugins
description: Put Claude Code, GitHub Copilot and Codex plugins under your lockfile, know what lands where, and work around today's known gaps.
---

Use this page when your team relies on a plugin and you want every teammate
and CI run on the same version, with hooks and MCP servers reviewed before
they run. A **plugin** is a folder with a `plugin.json` manifest that bundles
skills, agents, commands, hooks and MCP servers for a harness (the agent tool
that loads it: Claude Code, GitHub Copilot or Codex).

## Install a plugin

Install it like any package, from its repository or from a
[marketplace](../installing-from-marketplaces/):

```bash frame="terminal"
apm install contoso/review-kit#v2.0.0
apm install review-kit@contoso
```

Commit `apm.yml` and `apm.lock.yaml`, plus the [native plugin files](#commit-native-plugins)
APM writes. Teammates run `apm install`.

## What APM records and checks

| Control | What happens today |
|---|---|
| Lock | `apm.lock.yaml` pins the plugin's commit and records a SHA-256 hash for every file APM deployed. `apm audit` reports drift. |
| Scan | Every file is scanned for hidden Unicode at install; critical findings block. APM does not inspect what the code does. |
| Policy | Your organization's `apm-policy.yml` can allow or deny the source before anything is written. |
| Approve | Hooks, MCP servers, LSP servers and `bin/` deploy unless your project or organization turns on the approval gate; then they wait for `apm approve`. A plugin APM installs whole is enabled only when one approval pinned to its content covers it ([Approve hooks and MCP servers](../approve-executables/)). |

APM decides what gets installed. The harness decides what runs, with its own
permissions; APM is not a sandbox.

## Where plugins land

What APM does depends on the plugin format:

| Plugin format | What APM does today |
|---|---|
| Claude Code plugin (`.claude-plugin/plugin.json`), targets `claude`, or `copilot` at project scope | Deploys the publisher's files whole to `.agents/plugins/<plugin>/` (`~/.agents/plugins/` with `-g`) and declares them in each harness's settings, so the harness loads a real plugin. See [Native plugins](#native-plugins). A plugin with only a root `plugin.json` (an `apm pack` bundle), or a dependency with a `skills:` selection, deploys loose at project scope and is parked with `-g`. |
| Claude Code plugin, other targets | Splits it into primitives for each selected harness, in that harness's own skill, agent, command, hook and MCP paths. |
| Agent Plugin (`plugin.json` with the Agent Plugins `$schema`) | For GitHub Copilot: deploys it whole to `.agents/plugins/<plugin>/` and declares it in `.github/copilot/settings.json` (`~/.copilot/settings.json` with `-g`); Copilot CLI 1.0.81 or later loads it. A target list without `copilot` refuses it. Its root `mcp.json` is an executable (`mcp`, plus `bin` for a server that runs a command); approve the whole plugin with `apm approve <package>` ([details](../../reference/cli/approve/)). |
| Codex bundle (`.codex-plugin/plugin.json`) | With `apm install -g --target codex` and Codex CLI 0.163.0 or later: registers the whole bundle with Codex at user scope, disabled until you enable it ([details](../installing-from-marketplaces/#codex-compatibility-bundles-through-apm)). |

Exact paths per harness: [Targets matrix](../../reference/targets-matrix/).

## Native plugins

New in 0.37. APM installs a plugin whole for Claude Code and GitHub Copilot,
in your project or, with `-g`, for your user account:

```bash frame="terminal"
apm install review-kit@contoso
apm install -g --target claude review-kit@contoso
```

APM downloads into `apm_modules/`; harnesses load from `.agents/`.

:::caution[Where native plugins do not load]
Native plugins are reported not to load in git worktrees, in non-interactive
`claude -p` runs (including claude-code-action), and in Copilot CLI 1.0.106.
After upgrading to 0.37 or later, a dependency's skills can disappear there.
For a Claude-format plugin with skills, installed at project scope, deploy its
skills loose instead: add `skills: ["*"]` to the dependency in `apm.yml`; every
skill the package ships is installed, including ones added in later releases.
This does not apply with `-g` (a `skills:` selection parks the plugin), to Agent
Plugins (they stay native), or to a plugin without skills:

```yaml
dependencies:
  apm:
    - git: contoso/review-kit
      skills: ["*"]
```
:::

| | Project | User (`-g`) |
|---|---|---|
| Plugin folder | `.agents/plugins/<plugin>/` | `~/.agents/plugins/<plugin>/` |
| Claude Code settings (Claude plugins targeting `claude`) | `.claude/settings.json` | `~/.claude/settings.json` (honors `CLAUDE_CONFIG_DIR`) |
| GitHub Copilot settings | `.github/copilot/settings.json` (Claude plugins and Agent Plugins targeting `copilot`) | `~/.copilot/settings.json` (honors `COPILOT_HOME`; Agent Plugins only) |
| Marketplace name and path | `apm-p-<hash>`, `./.agents/plugins` | `apm-u-<hash>`, absolute path |

- **Whole plugin, publisher's bytes.** APM copies exactly the plugin's files
  (without `.git`, a root `apm.yml` or a root `.apm/`) to the plugin folder and
  lists them in a generated `.agents/plugins/.claude-plugin/marketplace.json`.
  At project scope it also writes `.agents/plugins/.gitattributes` (`* -text`),
  so a checkout with `core.autocrlf` keeps the bytes the lock pins. APM keeps
  your own file there, but refuses to deploy unless it has a `* -text` (or
  `* binary`) rule that no later rule setting `text` overrides.
  `${CLAUDE_PLUGIN_ROOT}` in commands and hooks resolves to that folder.
- **Declared, not installed by the harness.** APM writes only
  `extraKnownMarketplaces.<name>` and `enabledPlugins["<plugin>@<name>"]: true`.
  It needs no `claude` or `copilot` command. `/plugin` lists the plugin under
  that marketplace name.
- **One name per repository.** The project name hashes the `origin` remote as
  `host/owner/repo` (or the `apm.yml` name without a remote), so every clone
  agrees and other repositories differ. Once the lockfile records it, APM
  reuses it, so a fork keeps it. Claude Code keeps a per-user registry keyed by
  marketplace name; the per-repository name keeps repositories apart.
- **Enabled only when approved.** One approval, pinned to the plugin's file paths and
  bytes, covers the whole plugin, including its hooks, MCP servers, LSP servers and
  `bin/`. Run `apm approve <package>` (from `~/.apm` for `-g`), then install
  again. Until then the plugin is parked: not deployed, not declared, one
  warning, exit code 0.
- **`bin/` stays in the plugin.** It deploys inside the plugin folder, not under
  `.claude/skills`. It also needs `--trust-bin` (or an interactive install), and
  your organization's `bin_deploy` policy applies; otherwise the plugin is parked.
- **MCP and LSP servers stay in the plugin.** Claude Code and Copilot load them
  from the plugin, and APM writes no entry for them in those harnesses; other
  targets still get APM's copy. Your organization's MCP policy applies to each
  server: under `enforcement: block` a violation parks the plugin, under `warn`
  APM reports it and enables the plugin.
- **The lockfile owns it.** Each entry records `native_plugin`: the plugin name,
  its root, a fingerprint and the marketplace it was declared under. There is
  no other ownership file. The fingerprint covers every file's path and bytes,
  plus its executable bit at user scope only, so a project checkout without
  exec bits (Windows, `core.fileMode=false`) still passes; APM checks exec bits
  on each machine when it deploys from `apm_modules/`. `apm audit --ci` (from
  `~/.apm` with `claude` in its targets, for `-g`) reports drift when the
  source or the deployed copy no longer matches.
- **Dropping a target retires it.** A one-shot `--target` run or
  auto-detection keeps the record of a plugin it skips. When `targets:` drops
  every harness that loads a plugin, the next `apm install` retires it like any
  other dropped target's files: the root and its settings entries go, and an
  edited root is kept with a warning. `apm lock` leaves the plugin in place
  until that install, and `apm install --frozen` refuses the change. When
  `targets:` drops only some of those harnesses, the plugin stays and the next
  `apm install` removes the entries APM wrote in the dropped harness's
  settings (`--frozen` refuses); an entry you added there yourself is kept.
- **Your settings win.** APM adds only what the next lockfile needs and removes
  only entries the previous one produced. An explicit `false` in
  `enabledPlugins`, your own entries and a root you edited are kept. If the
  previous lockfile is missing, the binding moved, another marketplace already
  uses the name, or a settings file is not valid JSON, APM reports it and
  writes nothing. Settings, catalog and plugin folders change as one unit.
- **Uninstall is exact.** `apm uninstall` and `apm prune` remove the roots and
  keys the previous lockfile recorded, keep an edited root or an explicit
  `false`, and remove APM's marketplace entry once no plugins remain. If
  something is out of step, rerun `apm install` (`--global` for user scope)
  with the original `--target`.

APM parks, with a warning that names the package and the next step, a plugin with
a partial skill selection, symlinks, `monitors/` or `workflows/`, unknown manifest
capabilities, paths outside the plugin, or plugin `dependencies`. Other packages
in the same install continue. Two plugins whose names match when case is ignored
fail the install, naming both dependencies.

A plugin that previously had hooks installed loose is refused with
`Cannot switch <pkg> to a native plugin. Hook settings were preserved. Run: apm uninstall <pkg> && apm install <pkg>`
(add `--global` at user scope). A plugin without loose hooks switches on the
next install, and APM removes its stale loose files.

### Commit native plugins

Commit `.agents/plugins/`, `.claude/settings.json`,
`.github/copilot/settings.json` and `apm.lock.yaml`. A fresh clone then loads
the plugins in Claude Code and GitHub Copilot without APM, after the harness's
folder-trust prompt.

On each machine, a project-scope `apm install` also lists the project's
marketplace in Claude Code's user registry,
`~/.claude/plugins/known_marketplaces.json` (under `CLAUDE_CONFIG_DIR` when
set), with the checkout's absolute path. Without that entry, Claude Code's
first session on a machine (CI, `claude -p`, claude-code-action, a fresh clone)
starts a plugin's MCP server but not its skills or hooks. The write adds one
entry and changes no other; `apm uninstall` removes only the entry APM wrote.
If the registry already lists the project's marketplace for another checkout
(a second clone or worktree), cannot be parsed, or is reached through a
symbolic link, APM leaves it unchanged and warns; the plugins then load from
Claude Code's second session in that checkout.
Claude Code still loads nothing from a folder you have not trusted: accept its
folder-trust prompt once (headless runs record it as `hasTrustDialogAccepted`
in `~/.claude.json`).

### Claude Code and Copilot read each other's file

Copilot CLI also reads `.claude/settings.json`. When several files name the
same key, Copilot applies them in this order, later wins:
`.claude/settings.json`, `.claude/settings.local.json`,
`.github/copilot/settings.json`, `.github/copilot/settings.local.json`
(verified against Copilot CLI 1.0.112). So for a plugin that targets `claude`
but not `copilot`, APM writes `"<plugin>@apm-p-<hash>": false` into
`.github/copilot/settings.json` and Copilot leaves it off.

APM owns that Copilot key for every plugin it declares to Claude Code: it
flips it to `true` or `false`, or removes it, as the plugin's targets change,
and prints a `[!]` warning naming the key when it overrides a value it did not
write. A run whose `--target` excludes a native plugin leaves it exactly as
deployed, keeps its previous lock entry, and says the update waits.
To choose which harness loads a plugin, set the dependency's `targets:`. A
`false` you write for a plugin APM declares only to Copilot, or in Claude's
file, is kept.

### Upgrading from the `apm` marketplace

Earlier releases registered Agent Plugins under a marketplace named `apm`
pointing at `apm_modules/`, with an `apm_modules/.github/plugin/apm-registration.json`
ledger and keys in `.github/copilot/settings.local.json`. The first
`apm install` that targets `copilot` and declares every one of those plugins
from `.agents/plugins` removes that marketplace (only where it points at
`apm_modules/`) and its `true` keys, keeps explicit `false` and your own
entries, and then deletes the old ledger and catalog, in one rollback unit.
Any other command (`apm uninstall`, `apm prune`, `--target claude`) leaves the
old registration in place and warns you to run `apm install`. An unreadable
ledger is never deleted.

## Known gaps

| Gap | Effect | Workaround |
|---|---|---|
| Hooks or commands that expect the plugin root, on targets other than `claude` and `copilot`, where APM splits the plugin ([#2824](https://github.com/microsoft/apm/issues/2824), [#3191](https://github.com/microsoft/apm/issues/3191), [#3181](https://github.com/microsoft/apm/issues/3181)) | Hook or command fails or points at a missing path | Use the plugin through Claude Code or Copilot |
| Two plugins split for non-native targets ship a skill or agent with the same name ([#1120](https://github.com/microsoft/apm/issues/1120)) | Names collide | Install one of them, or select skills with `--skill` |
| A plugin that declares other plugins in `plugin.json` `dependencies` | Parked until APM can resolve plugin dependencies | Declare the dependencies in your `apm.yml` and exclude the plugin from `claude` and `copilot` |
| A git worktree whose main checkout also has the plugin ([anthropics/claude-code#111812](https://github.com/anthropics/claude-code/issues/111812)) | Claude Code loads the main checkout's copy; `apm install` in a worktree warns about this | Keep plugin changes in the main checkout, or start Claude Code there |
| Codex bundles | User scope, qualified on macOS arm64; the route is planned to change ([#3920](https://github.com/microsoft/apm/issues/3920)) | Use Codex's own `codex plugin add` |

:::note[Planned]
Whole-plugin installs for other harnesses, including Codex, are planned. Track
[#3094](https://github.com/microsoft/apm/issues/3094).
:::

## Verify

```bash frame="terminal"
apm deps list
apm audit
```

The plugin appears with its primitive counts, and `apm audit` reports no
drift.

## Plugin installed but missing in Claude Code

Work down this list:

1. **Is `claude` a target?** `apm install` opens with an `[i] Targets: ...` line.
   If `claude` is missing, add it to `targets:`
   ([Choose your harnesses](../choose-harnesses/)).
2. **Look for the plugin.** `/plugin` lists it under `apm-p-<hash>` (`apm-u-<hash>`
   at user scope), and `apm deps list` reports
   `N native plugin(s) activated from .agents/plugins`. If it is missing, the
   plugin may be parked (step 3), or `false` in `enabledPlugins` turned it off.
3. **Is something parked?** Run `apm approve --pending` (from `~/.apm` for user-scope
   plugins). Parked items need `apm approve <package>` and another install.
4. **Did Claude Code load the change?** Start it from the repository root,
   accept the folder-trust prompt and start a new session (or run
   `/reload-plugins`). Copilot CLI 1.0.81 or later loads the same plugins.
5. **Does a hook fail at runtime?** Check [Known gaps](#known-gaps).
6. **Is this an environment that does not load native plugins?** Native plugins
   are reported not to load in git worktrees, in non-interactive `claude -p`
   runs (including claude-code-action) and in Copilot CLI 1.0.106. `apm install`
   prints `[!] <package> installed as a native plugin ...` for each dependency
   it installs this way. For a Claude-format plugin with skills at project
   scope, the notice also says how to deploy its skills loose instead: set
   `skills: ["*"]` on that dependency in `apm.yml`. That does not apply with
   `-g` (a `skills:` selection parks the plugin), to Agent Plugins (they stay
   native) or to a plugin without skills, so their notice omits it.

:::tip[Other options]
Packaging your own plugin: [Build a plugin](../../producer/build-a-plugin/).
How APM classifies a `plugin.json`: [Package types](../../reference/package-types/).
:::
