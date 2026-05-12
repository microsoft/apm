---
title: "Primitives and Targets"
description: "The full reach map: every APM primitive against every supported harness."
sidebar:
  order: 3
---

A **primitive** is a unit of agent context APM can manage: instructions, prompts, agents, skills, hooks, commands, plugins, and MCP servers. A **target** is a harness APM compiles primitives for: Copilot, Claude, Cursor, Codex, Gemini, OpenCode, and Windsurf. The matrix below is the full reach map. For any primitive X and harness Y, it tells you whether Y receives X natively, receives it after APM transforms it, or does not receive it at all.

This page is the canonical reference. Tutorials and how-tos link here; do not duplicate.

## Primitive catalogue

APM authors place primitives under `.apm/` in their package. APM discovers, validates, and routes them at `apm install` (per-target) and `apm compile` (aggregated context files).

### Instructions

Coding standards and guidelines, scoped by file glob.

- Source: `.apm/instructions/*.instructions.md`
- Frontmatter: `description` (required), `applyTo` (optional glob)
- Deep dive: see [Anatomy of an APM Package](/apm/concepts/package-anatomy/)

### Prompts

Executable, parameterized AI workflows. Equivalent to a callable program for an LLM.

- Source: `.apm/prompts/*.prompt.md`
- Also surfaced as **commands** for harnesses that read slash-commands (see commands row below).
- Deep dive: [Prompts guide](/apm/producer/author-primitives/prompts/)

### Agents

Specialized AI personalities with tool boundaries and expertise scope.

- Source: `.apm/agents/*.agent.md` (legacy: `.chatmode.md`)
- Deep dive: [Agent workflows](/apm/producer/author-primitives/instructions-and-agents/)

### Skills

Cross-tool meta-guides authored in the agent-skills `SKILL.md` format. Bundled resources live alongside the skill.

- Source: `.apm/skills/<name>/SKILL.md` or root `SKILL.md`
- Deep dive: [Skills guide](/apm/producer/author-primitives/skills/)

### Hooks

Lifecycle event handlers (e.g. `PreToolUse`, `PostToolUse`, `Stop`) that invoke scripts.

- Source: `.apm/hooks/*.json` (or top-level `hooks/`)
- Deep dive: [MCP servers + hooks](/apm/consumer/install-mcp-servers/)

### Commands

Slash-commands for harnesses that expose a command palette. Sourced from `.apm/prompts/` -- there is no separate `.apm/commands/` directory. The same `.prompt.md` file becomes Copilot's prompt and Claude's `/command`.

### Plugins

A packaging format. A plugin is a self-contained bundle (`plugin.json` or `.claude-plugin/`) that ships a set of primitives. APM normalizes plugins at install time into the same primitives the rest of this page describes.

- Source: `plugin.json` at package root
- Deep dive: [Plugins guide](/apm/producer/author-primitives/)

### MCP servers

Model Context Protocol servers declared as dependencies. APM writes the per-harness MCP config file at install time.

- Source: `apm.yml` -> `dependencies.mcp:`
- Deep dive: [MCP servers](/apm/consumer/install-mcp-servers/)

## Target catalogue

Each target is identified by a slug used in `apm.yml`'s `targets:` field and on the `--target` flag. The output directory is where APM writes deployed primitives. The "agent-skills" and "copilot-cowork" targets exist in the registry but are not end-user runtimes; they are covered separately in the experimental reference.

| Slug | Output directory | Compile family |
|---|---|---|
| `copilot` | `.github/` (project), `~/.copilot/` (user scope) | vscode |
| `claude` | `.claude/` | claude |
| `cursor` | `.cursor/` | agents |
| `codex` | `.codex/` plus `.agents/` for skills | agents |
| `gemini` | `.gemini/` | gemini |
| `opencode` | `.opencode/` (project), `~/.config/opencode/` (user) | agents |
| `windsurf` | `.windsurf/` (project), `~/.codeium/windsurf/` (user) | agents |

Notes per target:

- **copilot** -- GitHub Copilot (CLI + IDE). User-scope partial: prompts and instructions are project-scope only.
- **claude** -- Claude Code. Full user-scope support. Hooks merge into `.claude/settings.json` rather than living as separate files.
- **cursor** -- Cursor IDE. Rules use the `.mdc` extension. Instructions are not deployable at user scope (Cursor exposes them via the Settings UI only).
- **codex** -- Codex CLI. Agents and hooks use TOML; skills use the cross-tool `.agents/` directory.
- **gemini** -- Gemini CLI. Commands are TOML. Hooks merge into `.gemini/settings.json`. No native agents or instructions primitives -- both arrive via compiled context files.
- **opencode** -- OpenCode. No hooks support.
- **windsurf** -- Windsurf / Cascade. Agents are delivered as auto-invokable skills under `.windsurf/skills/`. Workflows are the harness's name for commands.

## The compatibility matrix

Rows are primitives, columns are harnesses. Cell legend:

- **native** -- the harness reads this primitive directly from its own format and directory; APM writes the file as-is or in the harness's documented format.
- **compiled** -- APM transforms the primitive into a different format the harness understands (e.g. a prompt becomes a TOML command, an instruction is folded into `AGENTS.md`).
- **unsupported** -- APM does not deliver this primitive to this harness.
- **gated** -- delivered behind a flag or trust prompt.

| Primitive | Copilot | Claude | Cursor | Codex | Gemini | OpenCode | Windsurf |
|---|---|---|---|---|---|---|---|
| instructions | native | native | native | compiled | compiled | compiled | native |
| prompts | native | compiled | compiled | unsupported | compiled | compiled | compiled |
| agents | native | native | compiled | compiled | unsupported | native | compiled |
| skills | native | native | native | native | native | native | native |
| hooks | native | native | native | native | native | unsupported | native |
| commands | unsupported | native | compiled | unsupported | compiled | compiled | compiled |
| plugins | compiled | compiled | compiled | compiled | compiled | compiled | compiled |
| MCP servers | native | native | native | native | native | native | native |

How to read a cell:

- `instructions / claude = native` -- APM writes `.claude/rules/<name>.md`; Claude Code reads it directly.
- `prompts / claude = compiled` -- APM transforms `.apm/prompts/<n>.prompt.md` into `.claude/commands/<n>.md`. The prompt becomes a `/command`.
- `agents / gemini = unsupported` -- Gemini CLI has no agents primitive; APM does not deliver `.agent.md` files to it. Their content still reaches Gemini through the compiled `GEMINI.md` if referenced from instructions.
- `commands / copilot = unsupported` -- Copilot has no commands primitive; the same source `.prompt.md` reaches Copilot as a native prompt instead.
- `plugins / *` -- APM unpacks the plugin at install time into the primitives in the rows above; routing then follows those rows.
- `MCP servers / *` -- APM writes the harness's standard MCP config. Transitive MCP servers brought in by deep dependencies require an explicit trust prompt at install -- effectively `gated` for those, `native` for direct dependencies.

## Dev-only primitives

Mark a primitive as dev-only when it is useful to the package author but should not ship to consumers: release checklists, internal debugging agents, test-fixture skills, anything tied to your own infrastructure. Author such primitives outside `.apm/` (typically `dev/`) and reference them under `devDependencies` in `apm.yml`. `apm pack` excludes them; `apm install --dev` deploys them locally.

Full pattern, the three pack-time gotchas, and verification steps: [Dev-only primitives](/apm/producer/author-primitives/).

## How a target is selected

`apm install` and `apm compile` resolve active targets in this order:

1. Explicit `--target <slug>` flag, when passed.
2. The `targets:` field in `apm.yml`, when present.
3. Auto-detection: any harness whose root directory (`.github/`, `.claude/`, `.cursor/`, `.codex/`, `.gemini/`, `.opencode/`, `.windsurf/`) already exists in the workspace is selected.
4. Fallback: `minimal` -- APM writes `AGENTS.md` only and skips folder
   integration. Create one of the harness folders above (or set
   `targets:` explicitly) for full integration.

Unknown target slugs are rejected upstream by the manifest parser; they never silently fall through to the default.

For flag reference and exact resolution semantics, see [`apm compile` and `apm install`](/apm/reference/cli/install/). For policy controls that further restrict which primitives a target may deploy, see [Governance guide](/apm/enterprise/governance-guide/).
