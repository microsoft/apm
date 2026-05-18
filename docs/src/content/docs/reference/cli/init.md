---
title: apm init
description: Scaffold a new APM project by creating apm.yml (and optionally plugin.json) with auto-detected metadata.
sidebar:
  order: 1
---

## Synopsis

```bash
apm init [PROJECT_NAME] [OPTIONS]
```

## Description

Creates a minimal `apm.yml` in the current directory or in a new
`PROJECT_NAME` subdirectory. Auto-detects name, author, and description
so you can start running `apm install` immediately. Use `--plugin` to
also scaffold `plugin.json` for a publishable plugin, or `--marketplace`
to seed an authoring block for a marketplace.

## Arguments

| Argument | Description |
|---|---|
| `PROJECT_NAME` | Optional. Name of a new directory to create and `cd` into. Pass `.` to initialize in the current directory (same as omitting). Must not contain `/`, `\`, or be `..`. |

## Options

| Flag | Default | Description |
|---|---|---|
| `-y`, `--yes` | off | Skip interactive prompts; use auto-detected defaults. Overwrites an existing `apm.yml` without confirmation. |
| `--plugin` | off | Scaffold a plugin authoring project: also writes `plugin.json` and adds a `devDependencies` block to `apm.yml`. Plugin name must be kebab-case, max 64 chars. |
| `--marketplace` | off | Append a `marketplace:` authoring block to `apm.yml`. See [Publish to a marketplace](../../../producer/publish-to-a-marketplace/). |
| `--target` | (prompt) | Comma-separated target list. Skips the interactive target prompt and writes targets directly. Valid values: `copilot`, `claude`, `cursor`, `opencode`, `codex`, `gemini`, `windsurf`. |
| `-v`, `--verbose` | off | Show detailed output. |

Target precedence: `--target` flag > interactive prompt > auto-detect at
compile time (used with `--yes` or in non-TTY shells).

## Examples

Initialize in the current directory with prompts:

```bash
$ apm init
Setting up your APM project...
Project name: my-app
Version (1.0.0):
Description: My APM project
Author: alice
About to create:
  name: my-app
  targets: copilot, claude
Is this OK? [Y/n]: y
[+] APM project initialized successfully!
Created Files
  * apm.yml  Project configuration
```

Non-interactive scaffold of a new directory:

```bash
$ apm init my-app --yes
[*] Created project directory: my-app
[+] APM project initialized successfully!
Created Files
  * apm.yml  Project configuration
```

Plugin authoring project (creates `plugin.json` plus `apm.yml` with
`devDependencies`, version defaults to `0.1.0`):

```bash
$ apm init my-skill --plugin --yes
[+] APM project initialized successfully!
Created Files
  * apm.yml      Project configuration
  * plugin.json  Plugin metadata
```

Pin targets up front, skip the prompt:

```bash
$ apm init --yes --target copilot,claude,cursor
```

## Behavior

- **Files created:** `apm.yml` always. `plugin.json` when `--plugin` is
  set. The `marketplace:` block is appended to `apm.yml` when
  `--marketplace` is set.
- **Auto-detected fields:**
  - `name` -- from `PROJECT_NAME` or current directory name.
  - `author` -- from `git config user.name`, fallback `Developer`.
  - `description` -- generated from project name.
  - `version` -- `1.0.0` (or `0.1.0` with `--plugin --yes`).
- **Brownfield (existing `apm.yml`):** prints `[!] apm.yml already exists`
  and prompts to overwrite. With `--yes`, overwrites without asking.
- **Target seeding on re-init:** when `apm.yml` exists, the prompt
  pre-checks targets read from its existing `target:` field.
- **Codex hint:** if `.codex/` is present, suggests
  `--target agent-skills` to also deploy skills to `.agents/skills/`.
- **Exit codes:** `0` on success or user-aborted prompt; `1` on invalid
  project or plugin name, or unhandled error.

## Related

- [`apm install`](../install/) -- next step: install dependencies and
  deploy to targets.
- [Quickstart](../../../quickstart/) -- guided first project.
- [Concepts: package anatomy](../../../concepts/package-anatomy/) --
  what goes in `apm.yml`.
