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
| Approve | Hooks, MCP servers, LSP servers and `bin/` deploy unless your project or organization turns on the approval gate; then they wait for `apm approve`. From 0.37, a natively loaded plugin is approved as one unit pinned to its content ([Approve hooks and MCP servers](../approve-executables/)). |

APM decides what gets installed. The harness decides what runs, with its own
permissions; APM is not a sandbox.

## Where plugins land

What APM does depends on the plugin format:

| Plugin format | What APM does today |
|---|---|
| Claude Code plugin (`.claude-plugin/plugin.json`, or `plugin.json` without `$schema`) | Splits it into primitives for each selected harness: skills to `.claude/skills/` and `.agents/skills/`, agents and commands to `.claude/`, hooks into `.claude/settings.json` with their scripts under `.claude/hooks/<plugin>/`, MCP servers into `.mcp.json`. Claude Code sees the pieces, not a plugin. |
| Agent Plugin (`plugin.json` with the Agent Plugins `$schema`) | For GitHub Copilot: keeps it whole in `apm_modules/` and registers it in `.github/copilot/settings.local.json`, so Copilot CLI 1.0.81 or later loads it live. A target list without `copilot` refuses it. |
| Codex bundle (`.codex-plugin/plugin.json`) | With `apm install -g --target codex` and Codex CLI 0.163.0 or later: registers the whole bundle with Codex at user scope, disabled until you enable it ([details](../installing-from-marketplaces/#codex-compatibility-bundles-through-apm)). |

Exact paths per harness: [Targets matrix](../../reference/targets-matrix/).

## Known gaps

Splitting a Claude Code plugin changes its layout. These cases break or
arrive partial today:

| Gap | Effect | Workaround |
|---|---|---|
| Scripts that expect the plugin root, such as reading `../../.claude-plugin/plugin.json` or a skill path next to the hook ([#2824](https://github.com/microsoft/apm/issues/2824)) | Hook errors or injects the wrong content | Install that plugin natively with `/plugin install` |
| Hooks that read `process.env.CLAUDE_PLUGIN_ROOT` ([#3191](https://github.com/microsoft/apm/issues/3191)) | Hook fails silently | Same |
| `${CLAUDE_PLUGIN_ROOT}` inside commands ([#3181](https://github.com/microsoft/apm/issues/3181)) | Command points at a missing path | Same |
| Two plugins ship a skill or agent with the same name ([#1120](https://github.com/microsoft/apm/issues/1120)) | Names collide in `.claude/` | Install one of them, or select skills with `--skill` |
| A plugin that declares other plugins in `plugin.json` `dependencies` | APM reads the list as APM package references, not as the harness's plugin dependencies | Declare the dependencies in your `apm.yml` |
| Codex bundles | User scope, qualified on macOS arm64; the route is planned to change ([#3920](https://github.com/microsoft/apm/issues/3920)) | Use Codex's own `codex plugin add` |

:::note[Planned]
APM will deploy each approved plugin root whole to `.agents/plugins/` and
register it through each harness's documented settings keys, so Claude Code
and other harnesses load the plugin intact. This is not shipped. Track
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
2. **Look for the pieces, not the plugin.** `/plugin` in Claude Code does not
   list APM-installed Claude plugins. Check `.claude/skills/`,
   `.claude/agents/`, `.claude/commands/` and `.claude/settings.json`;
   `apm find .claude/skills/<name>/SKILL.md` names the package that
   wrote a file.
3. **Are hooks or MCP servers parked?** Run `apm approve --pending`. Parked
   items need `apm approve <package>` and another `apm install`.
4. **Did Claude Code load the change?** Start it from the repository root and
   start a new session. It asks you to approve project MCP servers from
   `.mcp.json`.
5. **Does a hook fail at runtime?** Check [Known gaps](#known-gaps).

:::tip[Other options]
Packaging your own plugin: [Build a plugin](../../producer/build-a-plugin/).
How APM classifies a `plugin.json`: [Package types](../../reference/package-types/).
:::
