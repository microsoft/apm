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
| `PROJECT_NAME` | Optional. Name of a new directory to create and `cd` into. Pass `.` to initialize in the current directory (same as omitting). Must be non-blank (not empty or whitespace-only), must not contain `/` or `\`, and must not be `..`. |

## Options

| Flag | Default | Description |
|---|---|---|
| `-y`, `--yes` | off | Skip interactive prompts; use auto-detected defaults. Overwrites an existing `apm.yml` without confirmation. |
| `--plugin` | off | **Deprecated.** Use [`apm plugin init`](../plugin/) instead. Scaffold a plugin authoring project: also writes `plugin.json` and adds a `devDependencies` block to `apm.yml`. Plugin name must be kebab-case, max 64 chars. |
| `--marketplace` | off | **Deprecated.** Use [`apm marketplace init`](../marketplace/) instead. Append a `marketplace:` authoring block to `apm.yml`. See [Publish to a marketplace](../../../producer/publish-to-a-marketplace/). |
| `--target` | (prompt) | Comma-separated target list. Skips the interactive target prompt. Stable manifest targets include `copilot`, `claude`, `grok-build`, `cursor`, `opencode`, `codex`, `gemini`, `antigravity`, `windsurf`, `kiro`, and `agent-skills`; `all` expands the default stable set. |
| `--discover` | off | Inventory existing agent-harness files (Claude Code, Copilot, Cursor, Codex, Gemini, Windsurf, Kiro, OpenCode, Grok Build) and propose an `apm.yml`. Read-only unless `--apply`. See [Discover existing agent context](#discover-existing-agent-context). |
| `--apply` | off | With `--discover`: apply the plan; convert hand-authored files into `.apm/` and create or merge `apm.yml`. Originals are never modified. `--write` is accepted as an alias. |
| `--format` | `text` | With `--discover`: `text`, `json`, or `yaml`. Machine formats keep stdout clean. |
| `-g`, `--global` | off | With `--discover`: scan the user scope (`~/.claude`, `~/.cursor`, `~/.codex`, ...) instead of the project, and write into `~/.apm/`. |
| `--include-hook-scripts` | off | With `--discover --apply`: copy in-project hook scripts into `.apm/hooks/scripts/` and rewrite hook commands to point at the copies. By default scripts are referenced, never copied or executed. |
| `-v`, `--verbose` | off | Show detailed output. |

Target precedence: `--target` flag > interactive prompt > auto-detect at
compile time (used with `--yes` or in non-TTY shells).

`init` writes only manifest-safe stable targets. For example, `--target agents`,
`--target vscode`, and the MCP-only `--target intellij` persist the canonical
`copilot` identifier, while `--target all` expands to the default stable set.
Experimental selectors such as `grok-cloud` are accepted by the shared CLI
target parser but are not persisted in `apm.yml`; enable them, then select them
with `apm install --target grok-cloud`.

## Discover existing agent context

`apm init --discover` is the inbound path for projects that already carry
agent configuration by hand. It reads back every location APM knows how to
deploy to (the same registry `apm install` writes with, inverted) plus the
root context files each harness loads implicitly, and classifies each hit.

```bash
$ apm init --discover
[>] Discovering agent context (project scope)
  TOOL     SCOPE    KIND          PATH                       IMPORT       OWNER       RISK  -> DEST
  claude   project  rule          .claude/rules/python.md    convertible  host-owned  -     .apm/instructions/python.instructions.md
  claude   project  hook          .claude/settings.json      convertible  host-owned  exec  .apm/hooks/claude-native.json
  claude   project  hook-script   .claude/hooks/notify.sh    reference-only host-owned exec -
  claude   project  mcp-server    .mcp.json#github           convertible  host-owned  exec  apm.yml#dependencies.mcp
  root     project  root-context  AGENTS.md                  ignored      apm-generated -   -
[i] Proposed apm.yml changes:
    targets: claude, copilot, cursor
    dependencies.mcp: github (from .mcp.json)
[i] Next steps:
    Preview only. Re-run with --apply to import into .apm/ and update apm.yml
```

Each finding carries:

| Column | Values |
|---|---|
| `KIND` | `instruction`, `rule`, `agent`, `prompt`, `command`, `skill`, `hook`, `hook-script`, `mcp-server`, `root-context`, `style`, `plugin`, `canvas`, `unknown` |
| `IMPORT` | `apm-native` (copied as is), `convertible` (frontmatter rewritten into APM's neutral form), `reference-only` (listed, never copied), `ignored` (already APM's, or not importable). Files containing credential-shaped tokens are refused with a reason; redact them first. |
| `OWNER` | `host-owned` (yours), `apm-owned` (recorded in `apm.lock.yaml`), `apm-generated` (compile output carrying an APM marker), `ambiguous` (mixed content; never written) |
| `RISK` | `exec` (runs code), `net` (talks to the network), `write` (changes files) |

Ownership is decided per file from `apm.lock.yaml`, recorded content hashes,
APM generation markers, and per-entry `_apm_source` hook markers, so a
project that already uses APM only sees its hand-authored remainder.

### What `--apply` does

1. Converts every `apm-native` or `convertible`, `host-owned` finding into the
   project's own `.apm/` layout (`instructions/`, `agents/`, `prompts/`,
   `skills/`, `hooks/`). The root `.apm/` is an implicit local package, so
   `apm install` deploys it like any dependency.
2. Rewrites vendor frontmatter into APM's neutral keys (always-on sources
   become `applyTo: "**"` so they stay unconditional on every target) and records every
   preserved, transformed, defaulted, or dropped field (field paths only,
   never values). Lossy conversions are printed under the file.
3. Creates `apm.yml` or merges into the existing one with comments preserved:
   `targets` gains the detected harnesses; MCP servers become self-defined
   `dependencies.mcp` entries. Literal credentials in MCP `env`, `headers`,
   arguments, or URLs are replaced by `${SERVER_KEY}` placeholders that
   `apm install` resolves from the environment.
4. Stages into a temporary directory and validates with the same parsers
   `apm install` uses *before* asking for confirmation, so the plan you approve
   lists every file with its conversion losses, the MCP servers and `apm.yml`
   edits, and anything skipped or refused. Files, `apm.yml` and provenance are
   then committed together and rolled back together on any error. If some items
   could not be imported the command still applies the rest but reports `PARTIAL`
   and exits with status 1 (`"status": "partial"` in JSON/YAML).
5. Writes `.apm/.import-sources.json` so a re-run is idempotent: unchanged
   sources are skipped, updated sources refresh their import, and an import
   you edited by hand is left alone with a warning.

`--apply` never deletes or edits the originals. After verifying the `.apm/`
copies, remove the originals yourself; until then both are loaded by the
source harness. `apm compile` regenerates `CLAUDE.md`, `AGENTS.md`, and
`GEMINI.md` from `.apm/instructions/` and will overwrite hand-authored root
files, so commit first.

### Migrating between harnesses

```bash
apm init --discover --apply --yes      # Claude Code project -> .apm/ + apm.yml
apm install --target cursor            # same context, rendered for Cursor
```

Hooks are stored in APM's neutral grammar as `.apm/hooks/<tool>-native.json`
and re-rendered per target. Events one harness cannot express are kept and
reported as pass-through so nothing is silently lost.

### Hidden alias

`apm discover [OPTIONS]` runs the same code as `apm init --discover`.

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
  - `name` -- from `PROJECT_NAME` or the current directory name. Falls back
    to `my-project` if the derived name is invalid (filesystem roots and
    other edge cases).
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
