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
so you can start running `apm install` immediately.

The legacy `--plugin` and `--marketplace` flags (which scaffold a
plugin or marketplace authoring block alongside `apm.yml`) are
deprecated but still accepted; use [`apm plugin init`](../plugin/)
and [`apm marketplace init`](../marketplace/) instead.

## Arguments

| Argument | Description |
|---|---|
| `PROJECT_NAME` | Optional. Name of a new directory to create and `cd` into. Pass `.` to initialize in the current directory (same as omitting). Must not contain `/`, `\`, or be `..`. |

## Options

| Flag | Default | Description |
|---|---|---|
| `-y`, `--yes` | off | Skip interactive prompts; use auto-detected defaults. Overwrites an existing `apm.yml` without confirmation. |
| `--plugin` | off | **Deprecated.** Use [`apm plugin init`](../plugin/) instead. Scaffold a plugin authoring project: also writes `plugin.json` and adds a `devDependencies` block to `apm.yml`. Plugin name must be kebab-case, max 64 chars. |
| `--marketplace` | off | **Deprecated.** Use [`apm marketplace init`](../marketplace/) instead. Append a `marketplace:` authoring block to `apm.yml`. See [Publish to a marketplace](../../../producer/publish-to-a-marketplace/). |
| `--target` | (prompt) | Comma-separated target list. Skips the interactive target prompt and writes targets directly. Valid values include `copilot`, `claude`, `cursor`, `opencode`, `codex`, `gemini`, `antigravity`, `windsurf`, `kiro`, `agent-skills`, and `all`. |
| `-v`, `--verbose` | off | Show detailed output. |

Target precedence: `--target` flag > interactive prompt > auto-detect at
compile time (used with `--yes` or in non-TTY shells).

`init` resolves CLI aliases before writing `targets:`. For example,
`--target agents` and `--target vscode` both persist the canonical
`copilot` identifier, while `--target all` expands to canonical manifest
targets. The generated manifest is therefore accepted unchanged by
`apm install`.

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
- **Existing plugin sources:** when plugin-native directories such as
  `skills/`, `agents/`, or `commands/` exist at the project root and `.apm/`
  does not, warns that they remain packable. `apm init` does not create
  `.apm/` automatically.
- **agentrc suggestion:** when no agent instruction files are found
  (`.github/copilot-instructions.md`, `AGENTS.md`, `.github/instructions/`),
  the Next Steps panel suggests generating agent instructions:
  - `agentrc` in PATH: prepends `Generate agent instructions: agentrc init`
    as the first next step.
  - `agentrc` not in PATH: prints a tip line with a link to
    `https://github.com/microsoft/agentrc`.
  - Instructions already exist: no mention (suppressed entirely).
- **Exit codes:** `0` on success or user-aborted prompt; `1` on invalid
  project or plugin name, or unhandled error.

## Deprecations

The `--plugin` and `--marketplace` flags are deprecated but remain
functional for compatibility. Each invocation prints a one-line warning
to stderr pointing at the replacement command (`apm plugin init` or
`apm marketplace init`). Migrate to:

- [`apm plugin init`](../plugin/) -- replaces `apm init --plugin`.
- [`apm marketplace init`](../marketplace/) -- replaces
  `apm init --marketplace`.

## Related

- [`apm plugin init`](../plugin/) -- scaffold a publishable plugin
  (replaces `apm init --plugin`).
- [`apm marketplace init`](../marketplace/) -- scaffold a marketplace
  authoring block (replaces `apm init --marketplace`).
- [`apm install`](../install/) -- next step: install dependencies and
  deploy to targets.
- [Quickstart](../../../quickstart/) -- guided first project.
- [Concepts: package anatomy](../../../concepts/package-anatomy/) --
  what goes in `apm.yml`.
