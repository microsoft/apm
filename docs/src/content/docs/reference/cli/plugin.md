---
title: apm plugin
description: Scaffold a publishable plugin project. The noun-verb home for `apm plugin init` and future plugin-scoped verbs.
sidebar:
  order: 21
---

## Synopsis

```bash
apm plugin init [PROJECT_NAME] [-y] [--target TARGETS] [--format FORMAT | --claude-plugin] [-v]

# Example
apm plugin init my-skill --yes
```

## Description

`apm plugin init` scaffolds a publishable plugin in the current directory: a `plugin.json` manifest plus an `apm.yml` carrying a `devDependencies` block. The result is a working tree you can commit, tag, and reference from a marketplace.

The generated dependency block is empty so direct installs select the plugin
layout. Adding an APM or MCP dependency makes `apm.yml` eligible and switches
direct installs to the APM package layout.

`apm plugin` is the noun-verb home for plugin-author workflows, mirroring `apm marketplace` for marketplace-author verbs. Today it ships a single verb -- `apm plugin init`. Sibling verbs live under the same namespace as they ship.

The two common repo shapes for plugin authors -- **single-plugin** (one plugin per repo) and **aggregator** (one repo that ships a marketplace plus the plugins it indexes) -- are not gated by flags. They emerge from composing `apm plugin init` and [`apm marketplace init`](../marketplace/#apm-marketplace-init) in the same working tree.

## Subcommands

### `apm plugin init`

Scaffold a plugin authoring project. Writes `plugin.json` and an `apm.yml` with a `devDependencies` block in the current directory (or under `PROJECT_NAME/` if provided). The `apm.yml` carries a commented `license:` line: APM does not choose a license for you, and `apm pack` warns until you set one.

```bash
apm plugin init
apm plugin init my-skill --yes
apm plugin init my-skill --target copilot,claude --yes
```

| Flag | Description |
|---|---|
| `PROJECT_NAME` | Optional positional. If provided, scaffolds into a new subdirectory of that name; otherwise writes into the current directory. Must be 1-64 lowercase letters, numbers, dots, or hyphens, begin and end with a letter or number, and not contain `..` or `--`. If a filesystem root has no directory name, APM uses `my-project`. |
| `--yes`, `-y` | Skip interactive prompts and use auto-detected defaults. |
| `--target` | Comma-separated target list (e.g. `copilot,claude,codex`). Skips the target prompt and writes selections directly. |
| `--claude-plugin` | Scaffold the Claude-compatible layout. This is what `apm plugin init` writes with zero flags. |
| `--format [plugin\|agent-plugin\|claude\|claude-plugin]` | `agent-plugin` scaffolds a portable [Agent Plugins v1](../pack/#agent-plugin-bundle---format-agent-plugin) manifest instead; `plugin`, `claude`, and `claude-plugin` all select the current Claude-compatible default. Mutually exclusive with `--claude-plugin`. |
| `--version` | Plugin version written to `apm.yml` and `plugin.json` (default `1.0.0`, or `0.1.0` with `--yes`). |
| `--author` | Plugin author (default: `git config user.name`). |
| `--description` | Plugin description (default: auto-detected). |
| `--verbose`, `-v` | Show detailed output. |

## Migration from `apm init --plugin`

`apm init --plugin` was removed in 0.37: it exits `2` without writing anything and names `apm plugin init`. Run `apm plugin init` instead, with the same `--version`, `--author` and `--description` options.

## Examples

### Single-plugin repo

One repo, one plugin. Author publishes a git tag; consumers reference it as `owner/repo@version`.

```bash
mkdir my-skill && cd my-skill
apm plugin init --yes
git init && git add . && git commit -m "init"
git tag v0.1.0
```

### Aggregator repo

One repo that ships a marketplace and the plugins it indexes side-by-side. Useful when you want one place to govern a small fleet of related plugins.

```bash
mkdir agents-hub && cd agents-hub
apm marketplace init
apm plugin init review-bot --yes
apm plugin init lint-bot --yes
```

The top-level `apm.yml` carries the marketplace authoring config; each plugin lives in its own subdirectory with its own `plugin.json` and `apm.yml`.

## See also

- [`apm marketplace`](../marketplace/) -- author and publish marketplaces that index your plugins.
- [`apm pack`](../pack/) -- build the Claude plugin bundle or the portable Agent Plugins v1 bundle from a scaffolded project.
