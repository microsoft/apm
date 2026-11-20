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

Commit `apm.yml` and `apm.lock.yaml`. Teammates run `apm install`.

## What APM records and checks

| Control | What happens today |
|---|---|
| Lock | `apm.lock.yaml` pins the plugin's commit and records a SHA-256 hash for every file APM deployed. `apm audit` reports drift. |
| Scan | Every file is scanned for hidden Unicode at install; critical findings block. APM does not inspect what the code does. |
| Policy | Your organization's `apm-policy.yml` can allow or deny the source before anything is written. |
| Approve | Hooks, MCP servers, LSP servers and `bin/` deploy unless your project or organization turns on the approval gate; then they wait for `apm approve`. A plugin installed whole (Claude Code at user scope, Claude Code or Copilot at project scope) is enabled only when one approval pinned to its content covers it ([Approve hooks and MCP servers](../approve-executables/)). |

APM decides what gets installed. The harness decides what runs, with its own
permissions; APM is not a sandbox.

## Where plugins land

What APM does depends on the plugin format:

| Plugin format | What APM does today |
|---|---|
| Claude Code plugin at user scope (`apm install -g --target claude`) | Deploys the publisher's files whole to `~/.agents/plugins/<plugin>/` and registers them in Claude Code's settings, so Claude Code loads a real plugin. See [Claude Code plugins at user scope](#claude-code-plugins-at-user-scope). |
| Claude Code plugin at project scope (`.claude-plugin/plugin.json`), when the project targets `claude` or `copilot` | Deploys it whole to the committed `.agents/plugins/<plugin>/` and declares it in `.claude/settings.json` and/or `.github/copilot/settings.json`, so a fresh clone loads it without APM. See [Plugins at project scope](#plugins-at-project-scope). |
| Other plugins at project scope (`plugin.json` without `$schema`, or no `claude`/`copilot` target) | Splits it into primitives for each selected harness: skills to `.claude/skills/` and `.agents/skills/`, agents and commands to `.claude/`, hooks into `.claude/settings.json` with their scripts under `.claude/hooks/<plugin>/`, MCP servers into `.mcp.json`. The harness sees the pieces, not a plugin. |
| Agent Plugin (`plugin.json` with the Agent Plugins `$schema`) | For GitHub Copilot: deploys it whole to `.agents/plugins/<plugin>/` and declares it in the committed `.github/copilot/settings.json` (`~/.agents/plugins` and `~/.copilot/settings.json` with `-g`), so Copilot CLI loads it live. A target list without `copilot` refuses it. Registrations an earlier APM wrote to `.github/copilot/settings.local.json` are removed on the next install. |
| Codex bundle (`.codex-plugin/plugin.json`) | With `apm install -g --target codex` and Codex CLI 0.163.0 or later: registers the whole bundle with Codex at user scope, disabled until you enable it ([details](../installing-from-marketplaces/#codex-compatibility-bundles-through-apm)). |

Exact paths per harness: [Targets matrix](../../reference/targets-matrix/).

## Claude Code plugins at user scope

New in 0.37. Install a Claude Code plugin for your user account and APM keeps it whole:

```bash frame="terminal"
apm install -g --target claude review-kit@contoso
```

- **Whole plugin, publisher's bytes.** APM copies exactly the plugin's files to
  `~/.agents/plugins/<plugin>/` and lists them in a generated
  `~/.agents/plugins/.claude-plugin/marketplace.json`. Its own normalization stays in
  its cache. `${CLAUDE_PLUGIN_ROOT}` in commands and hooks resolves to that folder.
- **Registered, not installed by Claude.** In Claude Code's `settings.json` (honoring
  `CLAUDE_CONFIG_DIR`), APM writes only `extraKnownMarketplaces.apm-u-<hash>` and
  `enabledPlugins["<plugin>@apm-u-<hash>"]: true`. It needs no `claude` command.
  `/plugin` lists the plugin under that marketplace name.
- **Enabled only when approved.** One approval, pinned to the plugin's file paths and
  bytes, covers the whole plugin, including its hooks, MCP servers, LSP servers and
  `bin/`. Run `cd ~/.apm && apm approve <package>`, then install again. Until then
  the plugin is parked: not deployed, not declared, one warning, exit code 0.
- **`bin/` stays in the plugin.** It deploys inside the plugin folder, not under
  `~/.claude/skills`. It also needs `--trust-bin` (or an interactive install), and
  your organization's `bin_deploy` policy applies; otherwise the plugin is parked.
- **MCP and LSP servers stay in the plugin.** Claude Code loads them from the plugin,
  and APM writes no Claude entry for them; other targets still get APM's copy. Your
  organization's MCP policy applies to each server: under `enforcement: block` a
  violation parks the plugin, under `warn` APM reports it and enables the plugin.
- **The lockfile owns it.** Each entry records `native_plugin`: the plugin name, its
  root, a fingerprint over the path, bytes and executable bit of every plugin file
  except APM's own `apm.yml` and `.apm/`, and the marketplace it was declared under.
  The record stays when a later run does not target `claude`. `apm audit --ci`, run from `~/.apm` with `claude`
  in its targets, reports drift when the source or the deployed copy no longer
  matches.
- **Your settings win.** An explicit `false` in `enabledPlugins`, your own entries and
  a root you edited are kept. If the previous lockfile is missing, the binding moved
  or another marketplace already uses the name, APM reports it and writes nothing.
- **Uninstall is exact.** `apm uninstall -g` removes the roots and `enabledPlugins`
  keys the previous lockfile recorded, keeps an edited root or an explicit `false`,
  and removes APM's marketplace entry once no plugins remain. If something is out of
  step, rerun `apm install --global` with the original `--target`.

APM parks, with a warning that names the package and the next step, a plugin with
a partial skill selection, symlinks, `monitors/` or `workflows/`, unknown manifest
capabilities, paths outside the plugin, names that collide when case is ignored, or
plugin `dependencies`. Other packages in the same install continue.

## Plugins at project scope

New in 0.38. In a project that targets `claude` or `copilot`, `apm install`
keeps a Claude Code plugin whole and commits it, so teammates and CI load it
without APM:

- **Committed root.** APM copies the approved plugin files from `apm_modules/`
  (still ignored by git) to `.agents/plugins/<plugin>/` and generates
  `.agents/plugins/.claude-plugin/marketplace.json`. Commit both.
- **Relative declaration.** APM writes `extraKnownMarketplaces.apm-p-<hash>`
  with the path `./.agents/plugins` and `enabledPlugins["<plugin>@apm-p-<hash>"]: true`
  to `.claude/settings.json` when the dependency targets Claude, and to
  `.github/copilot/settings.json` when it targets Copilot. The name comes from
  the `name` in `apm.yml`: the same in every clone, different per project.
- **Portable lock fingerprint.** The `native_plugin` record covers each file's
  path and bytes, not the executable bit, so a Windows checkout is not drift.
- **Same rules as user scope.** Approval, parking, your `false`, exact uninstall
  and refusals work as above. A plugin APM cannot install whole (a skill
  selection, symlinks, unknown capabilities) keeps its split install, with a
  warning naming why. A package whose hooks an earlier split install wrote must
  be removed with `apm uninstall <package>` and installed again.

Copilot CLI also reads `.claude/settings.json` and `.claude/settings.local.json`.
When files disagree on a plugin, the later one in this order wins (observed on
Copilot CLI 1.0.112): `.claude/settings.json`, `.claude/settings.local.json`,
`.github/copilot/settings.json`, `.github/copilot/settings.local.json`. To turn a
plugin off only for yourself, set it to `false` in a `.local` file.

Claude Code resolves the relative path against the main checkout, also from a
git worktree, and loads a newly declared plugin from the next session.

## Known gaps

APM still splits a plugin it cannot install whole, which changes its layout.
These cases break or arrive partial there; a plugin installed whole is not
affected:

| Gap | Effect | Workaround |
|---|---|---|
| Scripts that expect the plugin root, such as reading `../../.claude-plugin/plugin.json` or a skill path next to the hook ([#2824](https://github.com/microsoft/apm/issues/2824)) | Hook errors or injects the wrong content | Target `claude` or `copilot` so APM installs it whole |
| Hooks that read `process.env.CLAUDE_PLUGIN_ROOT` ([#3191](https://github.com/microsoft/apm/issues/3191)) | Hook fails silently | Same |
| `${CLAUDE_PLUGIN_ROOT}` inside commands ([#3181](https://github.com/microsoft/apm/issues/3181)) | Command points at a missing path | Same |
| Two plugins ship a skill or agent with the same name ([#1120](https://github.com/microsoft/apm/issues/1120)) | Names collide in `.claude/` | Install one of them, or select skills with `--skill` |
| A plugin that declares other plugins in `plugin.json` `dependencies` | APM reads the list as APM package references, not as the harness's plugin dependencies | Declare the dependencies in your `apm.yml` |
| Codex bundles | User scope, qualified on macOS arm64; the route is planned to change ([#3920](https://github.com/microsoft/apm/issues/3920)) | Use Codex's own `codex plugin add` |

:::note[Planned]
Whole-plugin installs ship for Claude Code at user scope in 0.37, and for
Claude Code and Copilot at project scope in 0.38. Other harnesses are planned.
Track [#3094](https://github.com/microsoft/apm/issues/3094).
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
2. **Look for the plugin or its pieces.** At user scope, `/plugin` lists it under
   `apm-u-<hash>`, and at project scope under `apm-p-<hash>`; if it is missing,
   the plugin may be parked (step 3). For a split plugin, check `.claude/skills/`,
   `.claude/agents/`, `.claude/commands/` and `.claude/settings.json`;
   `apm find .claude/skills/<name>/SKILL.md` names the package that
   wrote a file.
3. **Is something parked?** Run `apm approve --pending` (from `~/.apm` for user-scope
   plugins). Parked items need `apm approve <package>` and another install.
4. **Did Claude Code load the change?** Start it from the repository root and
   start a new session. It asks you to approve project MCP servers from
   `.mcp.json`.
5. **Does a hook fail at runtime?** Check [Known gaps](#known-gaps).

:::tip[Other options]
Packaging your own plugin: [Build a plugin](../../producer/build-a-plugin/).
How APM classifies a `plugin.json`: [Package types](../../reference/package-types/).
:::
