---
title: apm plugin
description: Scaffold a publishable plugin project. The noun-verb home for `apm plugin init` and future plugin-scoped verbs.
sidebar:
  order: 21
---

## Synopsis

```bash
apm plugin init [PROJECT_NAME] [-y] [--target TARGETS] [-v]

# Example
apm plugin init my-skill --yes
```

## Description

`apm plugin init` scaffolds a publishable plugin in the current directory: a `plugin.json` manifest plus an `apm.yml` carrying a `devDependencies` block. The result is a working tree you can commit, tag, and reference from a marketplace.

`apm plugin` is the noun-verb home for plugin-author workflows, mirroring `apm marketplace` for marketplace-author verbs. Today it ships a single verb -- `apm plugin init`. Sibling verbs live under the same namespace as they ship.

The two common repo shapes for plugin authors -- **single-plugin** (one plugin per repo) and **aggregator** (one repo that ships a marketplace plus the plugins it indexes) -- are not gated by flags. They emerge from composing `apm plugin init` and [`apm marketplace init`](../marketplace/#apm-marketplace-init) in the same working tree.

## Subcommands

### `apm plugin init`

Scaffold a plugin authoring project. Writes `plugin.json` and an `apm.yml` with a `devDependencies` block in the current directory (or under `PROJECT_NAME/` if provided).

```bash
apm plugin init
apm plugin init my-skill --yes
apm plugin init my-skill --target copilot,claude --yes
```

| Flag | Description |
|---|---|
| `PROJECT_NAME` | Optional positional. If provided, scaffolds into a new subdirectory of that name; otherwise writes into the current directory. Must be non-empty kebab-case without path separators and must not be `..`. If a filesystem root has no directory name, APM uses `my-project`. |
| `--yes`, `-y` | Skip interactive prompts and use auto-detected defaults. |
| `--target` | Comma-separated target list (e.g. `copilot,claude,codex`). Skips the target prompt and writes selections directly. |
| `--verbose`, `-v` | Show detailed output. |

## Migration from `apm init --plugin`

If you've used `apm init --plugin` before, here's the move: run `apm plugin init` instead. The generated files are byte-for-byte identical.

The legacy `apm init --plugin` flag still works and still produces the same output, but prints a deprecation warning on stderr.

Use `apm plugin init` for new plugin projects; keep the legacy flag only for compatibility scripts.

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
apm marketplace init --yes
apm plugin init review-bot --yes
apm plugin init lint-bot --yes
```

The top-level `apm.yml` carries the marketplace authoring config; each plugin lives in its own subdirectory with its own `plugin.json` and `apm.yml`.

## See also

- [`apm marketplace`](../marketplace/) -- author and publish marketplaces that index your plugins.
