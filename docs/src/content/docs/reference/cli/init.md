---
title: apm init
description: Create an APM project or discover existing local packages.
sidebar:
  order: 1
---

## Synopsis

```bash
apm init [PROJECT_NAME] [OPTIONS]
apm init --discover [OPTIONS]
```

## Description

Creates a minimal `apm.yml` in the current directory or in a new
`PROJECT_NAME` subdirectory. Auto-detects name, author, and description
so you can start running `apm install` immediately.

With `--discover`, inventories existing skill and package
directories in recognized layouts. It
does not copy, translate, or execute their content. Add `--apply` (or `--write`)
to merge missing local package references into `apm.yml`.

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
| `-y`, `--yes` | off | Skip interactive prompts. Plain `apm init --yes` keeps its existing behavior and overwrites an existing `apm.yml`; with `--discover --apply`, it consents to a merge instead. |
| `--discover` | off | Report admissible existing skill and package directories without changing files. Unsupported loose native rules, hooks, MCP configuration, and similar files are reported but not translated. |
| `--format text\|json\|yaml` | `text` | Select discovery report format. Used with `--discover`. |
| `--apply`, `--write` | off | After discovery, create or merge the consumer `apm.yml` with missing `dependencies.apm` local path references. Requires consent unless `--yes` is set. |
| `-g`, `--global` | off | Discover user-scope packages. Applied local references use installer-compatible absolute or home-rooted paths, not project-relative paths. |
| `--plugin` | off | **Deprecated.** Use [`apm plugin init`](../plugin/) instead. Scaffold a plugin authoring project: also writes `plugin.json` and adds a `devDependencies` block to `apm.yml`. Plugin name must be kebab-case, max 64 chars. |
| `--marketplace` | off | **Deprecated.** Use [`apm marketplace init`](../marketplace/) instead. Append a `marketplace:` authoring block to `apm.yml`. See [Publish to a marketplace](../../../producer/publish-to-a-marketplace/). |
| `--target` | (prompt) | Comma-separated target list for normal initialization. It cannot be combined with `--discover`; select deployment targets on the later `apm install`. Stable manifest targets include `copilot`, `claude`, `grok-build`, `cursor`, `opencode`, `codex`, `gemini`, `antigravity`, `windsurf`, `kiro`, and `agent-skills`; `all` expands the default stable set. |
| `-v`, `--verbose` | off | Show detailed output. |

Target precedence: `--target` flag > interactive prompt > auto-detect at
compile time (used with `--yes` or in non-TTY shells).

`init` writes only manifest-safe stable targets. For example, `--target agents`,
`--target vscode`, and the MCP-only `--target intellij` persist the canonical
`copilot` identifier, while `--target all` expands to the default stable set.
Experimental selectors such as `grok-cloud` are accepted by the shared CLI
target parser but are not persisted in `apm.yml`; enable them, then select them
with `apm install --target grok-cloud`.

## Examples

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

Declare an existing skill as a local package, then use the normal installer:

```text
.claude/skills/review/
|-- SKILL.md
`-- references.md
```

```bash
apm init --discover
apm init --discover --apply
apm install --target copilot
```

The merge adds the missing local reference without moving the source:

```yaml
dependencies:
  apm:
    - path: ./.claude/skills/review
```

The ordinary install routes the shared skill to
`.agents/skills/review/`. APM does not convert the skill to another standard;
the author and target harness remain responsible for compliance.

## Behavior

- **Plain init files created:** `apm.yml` always. `plugin.json` when `--plugin` is
  set. The `marketplace:` block is appended to `apm.yml` when
  `--marketplace` is set.
- **Auto-detected fields:**
  - `name` -- from `PROJECT_NAME` or the current directory name. Falls back
    to `my-project` if the derived name is invalid (filesystem roots and
    other edge cases).
  - `author` -- from `git config user.name`, fallback `Developer`.
  - `description` -- generated from project name.
  - `version` -- `1.0.0` (or `0.1.0` with `--plugin --yes`).
- **Plain init with existing `apm.yml`:** prints `[!] apm.yml already exists`
  and prompts to overwrite. With `--yes`, overwrites without asking.
- **Discovery:** reports existing admissible skill or package directories.
  Loose native rules, hooks, MCP configuration, and other unsupported content
  remain unsupported; discovery never translates or executes content.
- **Structured reports:** JSON and YAML reports include `scope`, `root`,
  `manifest`, `findings`, `additions`, and `applied`. Each finding includes its
  path, kind, status, reason, and proposed dependency when applicable. Status
  values are `supported`, `already-declared`, `managed`, `unsupported`, and
  `unsafe`.
- **Discovery apply:** creates or merges the consumer `apm.yml`, adding only
  missing `dependencies.apm` local path references. It leaves source content in
  place and creates no `.apm/` copies. Reruns do not duplicate references or overwrite source files.
  Noninteractive apply requires `--yes`.
  Later installs still use the normal collision policy.
- **Sharing local references:** project-relative sources must exist at the same
  path for teammates who run `apm install`. Global references use absolute or
  home-rooted paths accepted by the installer.
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
- **Exit codes:** `0` on success or a declined plain-init prompt; `1` on invalid
  project or plugin name, refused discovery apply, declined discovery consent,
  or unhandled error. Discovery consent refusal leaves files unchanged.
  Invalid option combinations, including `--discover --target`, exit `2`.

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
