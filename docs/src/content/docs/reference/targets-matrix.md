---
title: Targets matrix
description: Per-harness deployment matrix - detection signals, deploy directories, supported primitives, and file conventions for every APM target.
sidebar:
  order: 6
---

The canonical reference for what APM deploys, where, for every supported
harness. Use this page to choose a target, debug an unexpected deploy
location, or confirm whether a primitive is supported on a given tool.

For background on the target model, see
[Primitives and targets](../../concepts/primitives-and-targets/). For
the runtime CLI surface, see [`apm targets`](../cli/targets/) and
[`apm compile`](../cli/compile/). For the primitive types themselves,
see [Primitive types](../primitive-types/).

## Summary

| Target          | Deploy root            | instructions | prompts | agents | skills | commands | hooks | mcp |
|-----------------|------------------------|:------------:|:-------:|:------:|:------:|:--------:|:-----:|:---:|
| copilot         | `.github/`             |     [x]      |   [x]   |  [x]   |  [x]   |   [ ]    |  [x]  | [x] |
| claude          | `.claude/`             |     [x]      |   [ ]   |  [x]   |  [x]   |   [x]    |  [x]  | [x] |
| cursor          | `.cursor/`             |     [x]      |   [ ]   |  [x]   |  [x]   |   [x]    |  [x]  | [x] |
| codex           | `.codex/` + `.agents/` |     [ ]      |   [ ]   |  [x]   |  [x]   |   [ ]    |  [x]  | [x] |
| gemini          | `.gemini/`             |     [ ]      |   [ ]   |  [ ]   |  [x]   |   [x]    |  [x]  | [x] |
| opencode        | `.opencode/`           |     [ ]      |   [ ]   |  [x]   |  [x]   |   [x]    |  [ ]  | [x] |
| windsurf        | `.windsurf/`           |     [x]      |   [ ]   |  [x]   |  [x]   |   [x]    |  [x]  | [x] |
| agent-skills    | `.agents/`             |     [ ]      |   [ ]   |  [ ]   |  [x]   |   [ ]    |  [ ]  | [ ] |

Skills always deploy to the cross-tool `.agents/skills/` directory by
default (see [Skills convergence](#skills-convergence) below). All other
primitives land under each target's own root.

`copilot-cowork` (Microsoft 365 Copilot) is gated behind an experimental
flag and not listed above. See [Experimental](../experimental/).

## Detection and resolution

`apm install` and `apm compile` resolve the active target list with this
priority:

1. `--target` / `--all` on the command line.
2. `targets:` in `apm.yml`.
3. Auto-detection from filesystem signals (table below).

If none of the above produce a target, the command falls back to
`copilot`. Use [`apm targets`](../cli/targets/) to preview the resolved
list before `compile` or `install`.

### Detection signal whitelist

| Target   | Signals (any one activates the target)        |
|----------|-----------------------------------------------|
| claude   | `.claude/` directory, or `CLAUDE.md` file     |
| copilot  | `.github/copilot-instructions.md` file        |
| cursor   | `.cursor/` directory, or `.cursorrules` file  |
| codex    | `.codex/` directory                           |
| gemini   | `.gemini/` directory, or `GEMINI.md` file     |
| opencode | `.opencode/` directory                        |
| windsurf | `.windsurf/` directory                        |

`agent-skills` and `copilot-cowork` are never auto-detected. Select them
explicitly with `--target`.

## copilot

GitHub Copilot (CLI and IDE).

- **Detection.** `.github/copilot-instructions.md`.
- **Deploy directory.** `.github/` at project scope; `~/.copilot/` at user scope.
- **Supported primitives.** instructions, prompts, agents, skills, hooks, mcp.
- **File conventions.**
  - instructions: `.github/instructions/<name>.instructions.md`
  - prompts: `.github/prompts/<name>.prompt.md`
  - agents: `.github/agents/<name>.agent.md`
  - skills: `.agents/skills/<name>/SKILL.md`
  - hooks: `.github/hooks/<name>.json`
  - generated: `.github/copilot-instructions.md` (compile output)
- **User scope.** Partial. `prompts` and `instructions` are not supported at user scope; user-scope deploys land under `~/.copilot/`, not `~/.github/`.

## claude

Claude Code.

- **Detection.** `.claude/` directory, or `CLAUDE.md`.
- **Deploy directory.** `.claude/` (project and user scope; user scope honors `CLAUDE_CONFIG_DIR` if set).
- **Supported primitives.** instructions, agents, skills, commands, hooks, mcp. (No `prompts`.)
- **File conventions.**
  - instructions: `.claude/rules/<name>.md`
  - agents: `.claude/agents/<name>.md`
  - commands: `.claude/commands/<name>.md`
  - skills: `.agents/skills/<name>/SKILL.md`
  - hooks: merged into `.claude/settings.json`
- **Compile output.** `CLAUDE.md` and per-rule files under `.claude/rules/`.

## cursor

Cursor.

- **Detection.** `.cursor/` directory, or legacy `.cursorrules` file.
- **Deploy directory.** `.cursor/`.
- **Supported primitives.** instructions, agents, skills, commands, hooks, mcp. (No `prompts`.)
- **File conventions.**
  - instructions: `.cursor/rules/<name>.mdc`
  - agents: `.cursor/agents/<name>.md`
  - commands: `.cursor/commands/<name>.md`
  - skills: `.agents/skills/<name>/SKILL.md`
  - hooks: `.cursor/hooks.json`
- **User scope.** Partial. `instructions` is excluded at user scope; Cursor reads global rules from its Settings UI rather than from disk.
- **Caveat.** Command files use the shared `claude_command` transformer today; Cursor-specific frontmatter keys (`author`, `mcp`, `parameters`, ...) are dropped at install time and surfaced via diagnostics.

## codex

OpenAI Codex CLI.

- **Detection.** `.codex/` directory.
- **Deploy directory.** `.codex/` plus `.agents/` for skills.
- **Supported primitives.** agents, skills, hooks, mcp. (No `instructions`, `prompts`, or `commands`.)
- **File conventions.**
  - agents: `.codex/agents/<name>.toml`
  - skills: `.agents/skills/<name>/SKILL.md`
  - hooks: `.codex/hooks.json`
- **Compile output.** `AGENTS.md` only. Per-file instructions are not installed for Codex.

## gemini

Gemini CLI.

- **Detection.** `.gemini/` directory, or `GEMINI.md`.
- **Deploy directory.** `.gemini/` (project and user scope).
- **Supported primitives.** commands, skills, hooks, mcp.
- **File conventions.**
  - commands: `.gemini/commands/<name>.toml`
  - skills: `.agents/skills/<name>/SKILL.md`
  - hooks: merged into `.gemini/settings.json`
- **Compile output.** `GEMINI.md`. Gemini CLI does not read per-file rules from `.gemini/rules/`, so `instructions` is compile-only.

## opencode

OpenCode.

- **Detection.** `.opencode/` directory.
- **Deploy directory.** `.opencode/` at project scope; `~/.config/opencode/` at user scope.
- **Supported primitives.** agents, commands, skills, mcp.
- **File conventions.**
  - agents: `.opencode/agents/<name>.md`
  - commands: `.opencode/commands/<name>.md`
  - skills: `.agents/skills/<name>/SKILL.md`
- **Caveat.** OpenCode has no hooks concept; the `hooks` primitive is silently skipped for this target.

## windsurf

Windsurf / Cascade.

- **Detection.** `.windsurf/` directory.
- **Deploy directory.** `.windsurf/` at project scope; `~/.codeium/windsurf/` at user scope.
- **Supported primitives.** instructions, agents, skills, commands, hooks, mcp.
- **File conventions.**
  - instructions: `.windsurf/rules/<name>.md`
  - agents: `.windsurf/skills/<name>/SKILL.md` (Cascade auto-invokes skill-shaped agents by description)
  - skills: `.windsurf/skills/<name>/SKILL.md`
  - commands: `.windsurf/workflows/<name>.md`
  - hooks: `.windsurf/hooks.json`
- **User scope.** Partial. `instructions` is excluded at user scope; Windsurf stores global memory in a single `~/.codeium/windsurf/memories/global_rules.md` file with a different format.

## agent-skills

Cross-client shared skills directory.

- **Detection.** Never auto-detected. Select with `--target agent-skills`.
- **Deploy directory.** `.agents/`.
- **Supported primitives.** skills only.
- **File conventions.** `.agents/skills/<name>/SKILL.md`.
- **Use case.** Author-time target for shipping a SKILL bundle that any Skills-aware client (Codex, Copilot CLI, Claude Code, etc.) can read without per-tool deployment.

## Skills convergence

By default, every target with a `skills` primitive deploys to `.agents/skills/<name>/SKILL.md` rather than under the target root. This matches the cross-tool agent skills convention so a single skill bundle serves every harness.

To restore the pre-convergence per-target layout (skills land under each target's own root), use the `--legacy-skill-paths` flag on `apm install` or set `APM_LEGACY_SKILL_PATHS=1`.

## MCP servers

MCP is not a `TargetProfile` primitive; it is wired by a separate integrator that writes per-client config files (e.g. `.vscode/mcp.json`, `.cursor/mcp.json`, `.claude.json`) for every target with an MCP client adapter. The matrix above marks `mcp` supported when an adapter exists. See [`apm mcp`](../cli/mcp/) for the runtime surface.

## See also

- [`apm targets`](../cli/targets/) - inspect resolved targets at runtime.
- [`apm compile`](../cli/compile/) - target selection and compile flags.
- [Primitive types](../primitive-types/) - what each primitive is.
- [Primitives and targets](../../concepts/primitives-and-targets/) - conceptual model.
