---
title: apm compile
description: Compile primitives into per-target deployment files
sidebar:
  order: 11
---

Compile your **instructions** primitives into the AGENTS.md / CLAUDE.md
/ GEMINI.md root context files (and per-harness rules trees) that each
agent harness reads at startup.

## Synopsis

```bash
apm compile [OPTIONS]
```

## Description

`apm compile` reads `instructions/*.instructions.md` from `.apm/`
(your project) and `apm_modules/` (installed dependencies), then
writes one set of root context files plus per-harness rules per
resolved target.

Compile only handles **instructions** (and optionally a single
chatmode to prepend via `--chatmode`). Other primitive types --
prompts, skills, agents, hooks, commands, MCP -- are deployed by
`apm install` directly into the harness directories that consume them
and are not touched by `apm compile`. See
[Primitives and targets](../../../concepts/primitives-and-targets/)
for the full reach map.

**When you actually need it:** compile is **optional for the
`copilot` target** -- GitHub Copilot natively reads
`.github/instructions/*.instructions.md` (with their `applyTo:`
frontmatter) that `apm install` already deploys. Compile is
**recommended for every other target** (`claude`, `cursor`, `codex`,
`gemini`, `opencode`, `windsurf`), which load instructions through a
root context file or harness-specific rules folder that compile
generates.

Resolution order for which targets to compile:

1. `--target` / `--all` on the command line
2. `targets:` field in `apm.yml`
3. Auto-detection from existing folders (`.github/`, `.claude/`,
   `.codex/`, `.gemini/`, `.windsurf/`)

Use [`apm targets`](../targets/) to preview what auto-detection
resolves to before compiling.

The compiled output is scanned for hidden Unicode before any file is
written. Critical findings cause the command to exit non-zero. See
[Drift and secure by default](../../../consumer/drift-and-secure-by-default/).

## Options

### Target selection

| Flag | Description |
|------|-------------|
| `-t, --target VALUE` | Target(s) to compile. Comma-separated. Values: `copilot`, `claude`, `cursor`, `opencode`, `codex`, `gemini`, `windsurf`, `agent-skills`, `all`. |
| `--all` | Compile for all canonical targets. Equivalent to `--target all` and mutually exclusive with `--target`. Preferred form. |

`vscode` and `agents` are accepted as deprecated aliases for `copilot`
and emit a one-line warning. `--target all` also emits a deprecation
warning -- prefer `--all`.

`agent-skills` is a no-op for `compile` (skills-only deployment target);
include it in `--target` lists when you also want shared
`.agents/skills/` output alongside another target.

### Output control

| Flag | Description |
|------|-------------|
| `-o, --output PATH` | Output file path. Only applies in single-file mode (`--single-agents`). Default: `AGENTS.md`. |
| `--single-agents` | Force single-file compilation (legacy). Writes one combined file at `--output` instead of distributed AGENTS.md tree. |
| `--clean` | Remove orphaned AGENTS.md files no longer produced by the current primitive set. |

### Content

| Flag | Description |
|------|-------------|
| `--chatmode NAME` | Prepend the named chatmode to the generated AGENTS.md. |
| `--no-links` | Skip markdown link resolution. |
| `--with-constitution` / `--no-constitution` | Include or omit the Spec Kit `memory/constitution.md` block at the top. Default: included. When disabled, an existing block is preserved but not regenerated. |
| `--local-only` | Ignore `apm_modules/`; compile only `.apm/` primitives. |
| `--legacy-skill-paths` | Deploy skills to per-client paths (e.g. `.cursor/skills/`) instead of the shared `.agents/skills/`. Compatibility flag. |

### Modes

| Flag | Description |
|------|-------------|
| `--watch` | Re-run compilation on file changes. See [Watch mode](#watch-mode). |
| `--validate` | Validate primitives and exit. No files written. |
| `--dry-run` | Show placement decisions without writing files. |
| `-v, --verbose` | Show source attribution and optimizer analysis. |

## Examples

Compile for whatever the project is set up for:

```bash
apm compile
```

Compile for one target:

```bash
apm compile --target claude
apm compile --target copilot
apm compile --target cursor
```

Compile for several targets in one pass:

```bash
apm compile -t claude,copilot
apm compile -t copilot,agent-skills
```

Compile for every canonical target:

```bash
apm compile --all
```

Validate without writing:

```bash
apm compile --validate
```

Preview placement:

```bash
apm compile --dry-run
apm compile -t claude,codex --dry-run -v
```

Skip dependencies and compile only local primitives:

```bash
apm compile --local-only
```

Remove stale AGENTS.md files after deleting primitives:

```bash
apm compile --clean
```

## Watch mode

`apm compile --watch` monitors the project for source changes and
re-runs compilation automatically.

- Watched directories (when present): `.apm/`, `.github/instructions/`,
  `.github/agents/`, `.github/chatmodes/`.
- Triggers on changes to `.md` files and `apm.yml`.
- 1-second debounce to coalesce rapid edits.
- Press Ctrl+C to stop.
- Combine with `--dry-run` to validate placement on every save without
  writing.

```bash
apm compile --watch
apm compile --watch --dry-run
```

Watch mode uses the single-file output path (`--output`); for
distributed compilation in watch mode, edit and re-run normally.

## Output layout per target

| Target | Files written |
|--------|---------------|
| `copilot` | `AGENTS.md`, `.github/copilot-instructions.md`, `.github/prompts/`, `.github/agents/`, `.github/skills/` |
| `claude` | `CLAUDE.md`, `.claude/commands/`, `.claude/skills/SKILL.md` |
| `cursor` | `AGENTS.md`, `.cursor/rules/`, `.cursor/skills/` |
| `codex` | `AGENTS.md`, `.codex/agents/`, `.codex/hooks.json`, `.agents/skills/` |
| `opencode` | `AGENTS.md`, `.opencode/agents/`, `.opencode/commands/`, `.opencode/skills/` |
| `gemini` | `GEMINI.md`, `.gemini/commands/`, `.gemini/skills/` |
| `windsurf` | `AGENTS.md`, `.windsurf/rules/`, `.windsurf/skills/`, `.windsurf/workflows/` |
| `agent-skills` | `.agents/skills/` only (cross-client shared skills) |
| `all` | All of the above except `agent-skills` |

`.github/copilot-instructions.md` is only managed by APM when its first
line is the marker `<!-- Generated by APM CLI from .apm/ primitives -->`.
A hand-authored file is left untouched on both write and cleanup paths.
To hand off an existing file to APM, prepend the marker (or delete the
file) and re-run `apm compile`.

## Strategy modes

There is no `--strategy` flag. Compilation runs in one of two modes:

- **Distributed (default)** -- writes a tree of focused AGENTS.md files
  next to the code they apply to, plus per-target subdirectories. This
  is the recommended mode and follows the Minimal Context Principle.
- **Single-file (`--single-agents`)** -- writes one combined file at
  `--output` (default `AGENTS.md`). Use when a harness or workflow
  requires a single context file.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Compilation succeeded (or `--validate` passed). |
| 1 | No `apm.yml`, no primitives to compile, validation failure, hidden-Unicode finding, or compilation error. |
| 2 | Conflicting flags (e.g. `--all` combined with `--target`). |

## Related

- [`apm install`](../install/) -- fetches dependencies into `apm_modules/` so `compile` can read them.
- [`apm targets`](../targets/) -- shows what targets resolve to in the current project.
- [Concepts: primitives and targets](../../../concepts/primitives-and-targets/)
- [Concepts: lifecycle](../../../concepts/lifecycle/)
- [Producer: compile](../../../producer/compile/) -- author workflow that calls `compile` with packaging-specific defaults.
