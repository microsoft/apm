---
title: apm list
description: List scripts defined in apm.yml
sidebar:
  order: 6
---

List the scripts declared in your project's `apm.yml`.

## Synopsis

```bash
apm list
```

## Description

`apm list` prints every entry from the `scripts:` mapping in the `apm.yml` at your current working directory. Scripts are short shell commands you invoke with [`apm run`](../run/) -- the same pattern as `npm run` in `package.json`.

This command does **not** list installed packages, dependencies, or files under `apm_modules/`. It only reads the `scripts:` section of the manifest.

If a script named `start` exists, it is marked as the default and runs when you call `apm run` with no script name.

:::note[Coming from npm?]
`apm list` is closer to `npm run` (with no arguments) than to `npm list`. There is no tree view of installed packages. To inspect what `apm install` placed on disk, look inside `apm_modules/` or read [`apm.lock.yaml`](../../lockfile-spec/).
:::

## Options

None. `apm list` takes no flags or arguments.

Global flags from the root command (`--verbose`, `--quiet`) apply.

## Examples

### List scripts in the current project

Given an `apm.yml` like:

```yaml
name: my-project
version: 0.1.0
scripts:
  start: "codex run main.prompt.md"
  fast: "llm prompt main.prompt.md -m github/gpt-4o-mini"
  debug: "RUST_LOG=debug codex run main.prompt.md"
```

Run:

```bash
apm list
```

Output:

```
 Available Scripts

  Script   Command
 -------------------------------------------------------------------
  [*] start    codex run main.prompt.md
      fast     llm prompt main.prompt.md -m github/gpt-4o-mini
      debug    RUST_LOG=debug codex run main.prompt.md

[i] [*] = default script (runs when no script name specified)
```

### Empty scripts section

If `apm.yml` has no `scripts:` entries, `apm list` prints a warning and shows an example block you can paste in:

```
[!] No scripts found.

Add scripts to your apm.yml file:
scripts:
  start: "codex run main.prompt.md"
  fast: "llm prompt main.prompt.md -m github/gpt-4o-mini"
```

Edit `apm.yml`, add at least one script, and re-run `apm list`.

## Related

- [`apm run`](../run/) -- execute a listed script.
- [`apm install`](../install/) -- install the dependencies your scripts invoke.
- [Package anatomy](../../../concepts/package-anatomy/) -- where `scripts:` sits in the manifest.
