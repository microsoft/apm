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
[Primitives and targets](../concepts/primitives-and-targets/). For
the runtime CLI surface, see [`apm targets`](./cli/targets/) and
[`apm compile`](./cli/compile/). For the primitive types themselves,
see [Primitive types](./primitive-types/).

## Summary

| Target          | Deploy root            | instructions | prompts | agents | skills | commands | hooks | mcp |
|-----------------|------------------------|:------------:|:-------:|:------:|:------:|:--------:|:-----:|:---:|
| copilot         | `.github/`             |     [x]      |   [x]   |  [x]   |  [x]   |   [ ]    |  [x]  | [x] |
| claude          | `.claude/`             |     [x]      |   [ ]   |  [x]   |  [x]   |   [x]    |  [x]  | [x] |
| cursor          | `.cursor/`             |     [x]      |   [ ]   |  [x]   |  [x]   |   [x]    |  [x]  | [x] |
| codex           | `.codex/` + `.agents/` |     [ ]      |   [ ]   |  [x]   |  [x]   |   [ ]    |  [x]  | [x] |
| gemini          | `.gemini/`             |     [ ]      |   [ ]   |  [ ]   |  [x]   |   [x]    |  [x]  | [x] |
| antigravity     | `.agents/`             |     [x]      |   [ ]   |  [ ]   |  [x]   |   [ ]    |  [x]  | [x] |
| opencode        | `.opencode/`           |     [ ]      |   [ ]   |  [x]   |  [x]   |   [x]    |  [ ]  | [x] |
| windsurf        | `.windsurf/` + `.agents/` |     [x]      |   [ ]   |  [ ]   |  [x]   |   [x]    |  [x]  | [x] |
| kiro            | `.kiro/`               |     [x]      |   [ ]   |  [ ]   |  [x]   |   [ ]    |  [x]  | [x] |
| agent-skills    | `.agents/`             |     [ ]      |   [ ]   |  [ ]   |  [x]   |   [ ]    |  [ ]  | [ ] |

Skills deploy to `.agents/skills/` for Copilot, Cursor, OpenCode,
Gemini, Antigravity, Codex, and Windsurf by default (see [Skills convergence](#skills-convergence)
below). Claude and Kiro keep target-native skill directories.

`copilot-cowork` (Microsoft 365 Copilot), `copilot-app` (GitHub
Copilot desktop App), `openclaw` (OpenClaw agent runtime), and `hermes` are
gated behind experimental flags and not listed above. See
[Experimental](./experimental/).

## Detection and resolution

`apm install` and `apm compile` resolve the active target list with this
priority:

1. `--target` / `--all` on the command line.
2. `targets:` in `apm.yml`.
3. Auto-detection from filesystem signals (table below).

If none of the above produce a target, the command falls back to
`copilot`. Use [`apm targets`](./cli/targets/) to preview the resolved
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
| kiro     | `.kiro/` directory                            |

`agent-skills` is a canonical target key; `antigravity` is explicit-only.
Both are available with `--target`, but only `agent-skills` can be listed in a
project's `apm.yml` `targets:` field so contributors running plain `apm
install` pick it up automatically.

`copilot-cowork`, `copilot-app`, `openclaw`, and `hermes` are experimental targets
that require `apm experimental enable <name>` before use. They are selected
with `--target` only and cannot be listed in `apm.yml` (the canonical
targets validator will reject them).

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
- **User scope.** Partial. `prompts` deploy under `~/.copilot/prompts/`; `instructions` from all packages are concatenated into `~/.copilot/copilot-instructions.md` (Copilot CLI reads only that single file at user scope). User-scope deploys land under `~/.copilot/`, not `~/.github/`.
- **Global compile.** `apm compile -g` can also render global instructions to
  `~/.copilot/AGENTS.md` for root-context readers that honor `AGENTS.md`.

## claude

Claude Code.

- **Detection.** `.claude/` directory, or `CLAUDE.md`.
- **Deploy directory.** `.claude/` (project and user scope; user scope honors `CLAUDE_CONFIG_DIR` if set).
- **Supported primitives.** instructions, agents, skills, commands, hooks, mcp. (No `prompts`.)
- **File conventions.**
  - instructions: `.claude/rules/<name>.md`
  - agents: `.claude/agents/<name>.md`
  - commands: `.claude/commands/<name>.md`
  - skills: `.claude/skills/<name>/SKILL.md`
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
- **Global compile.** `apm compile -g` can render global instructions to
  `~/.cursor/AGENTS.md` for root-context readers that honor `AGENTS.md`; Cursor
  global rules still use the Settings UI.
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

## antigravity

Google Antigravity CLI (`agy`), successor to Gemini CLI.

- **Detection.** None -- explicit-only. Antigravity shares the cross-tool `.agents/` root, so there is no unique auto-detect signal. Select it with `--target antigravity`; it is not part of `--target all` and is not accepted in `apm.yml` `targets:`. Project-scope MCP writes are opt-in: `.agents/` must already exist (APM does not create it automatically for MCP).
- **Deploy directory.** `.agents/` (project scope); `~/.gemini/` (user scope).
- **Supported primitives.** instructions, skills, hooks, mcp.
- **File conventions.**
  - instructions: `.agents/rules/<name>.md`
  - skills: `.agents/skills/<name>/SKILL.md`
  - hooks: `.agents/hooks.json` (Antigravity's native schema: `PreToolUse`/`PostToolUse`/`PreInvocation`/`PostInvocation`/`Stop`)
  - mcp: `.agents/mcp_config.json` (project; `mcpServers` key) or `~/.gemini/config/mcp_config.json` (user)
- **Compile output.** `AGENTS.md`.

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
- **Global compile.** `apm compile -g` writes
  `~/.config/opencode/AGENTS.md` from global instructions.

## windsurf

Windsurf / Cascade.

- **Detection.** `.windsurf/` directory.
- **Deploy directory.** Native primitives deploy under `.windsurf/` at project scope and `~/.codeium/windsurf/` at user scope; skills converge on `.agents/skills/` at both scopes (`~/.agents/skills/` at user scope).
- **Supported primitives.** instructions, skills, commands, hooks, mcp.
- **File conventions.**
  - instructions: `.windsurf/rules/<name>.md`
  - skills: `.agents/skills/<name>/SKILL.md`
  - commands: `.windsurf/workflows/<name>.md`
  - hooks: `.windsurf/hooks.json`
- **Agents.** Not deployed. Cascade auto-invokes any `SKILL.md` by its `description:` frontmatter, so a separate agents primitive would collide with skills on the same path. Ship personas as skills under `.apm/skills/<name>/SKILL.md` instead.
- **User scope.** Partial. `instructions` is excluded at user scope; Windsurf stores global memory in a single `~/.codeium/windsurf/memories/global_rules.md` file with a different format.

## kiro

Kiro IDE.

- **Detection.** `.kiro/` directory.
- **Deploy directory.** `.kiro/` (project and user scope).
- **Supported primitives.** instructions, skills, hooks, mcp.
- **File conventions.**
  - instructions: `.kiro/steering/<name>.md` with `inclusion: always` or `inclusion: fileMatch` frontmatter
  - skills: `.kiro/skills/<name>/SKILL.md`
  - hooks: one JSON file per hook action under `.kiro/hooks/`
  - mcp: `.kiro/settings/mcp.json` (project) or `~/.kiro/settings/mcp.json` (user)
- **MCP shape.** JSON `mcpServers` entries use `command`/`args`/`env` for stdio and `url`/`headers` for remote servers. Kiro resolves `${VAR}` placeholders at runtime, so APM preserves them rather than writing secrets to disk.
- **Scope.** This is the documented Kiro IDE layout only. Kiro CLI differences are tracked separately and are not part of this target.

## agent-skills

Cross-client shared skills directory.

- **Detection.** Never auto-detected. Select with `--target agent-skills`.
- **Deploy directory.** `.agents/`.
- **Supported primitives.** skills only.
- **File conventions.** `.agents/skills/<name>/SKILL.md`.
- **Use case.** Author-time target for shipping a SKILL bundle that any Skills-aware client (Codex, Copilot CLI, Claude Code, etc.) can read without per-tool deployment.

## openclaw (experimental)

[OpenClaw](https://github.com/openclaw/openclaw) agent runtime.

- **Detection.** Never auto-detected. Select with `--target openclaw`
  after enabling the experimental flag.
- **Enable.** `apm experimental enable openclaw`.
- **Deploy directory.** `.agents/skills/` at project scope (identical to
  `agent-skills`); `~/.openclaw/skills/` at user scope (`--global`).
- **Supported primitives.** skills only.
- **File conventions.** `.agents/skills/<name>/SKILL.md` (project) or
  `~/.openclaw/skills/<name>/SKILL.md` (user).
- **Note.** At project scope the output is identical to `agent-skills`.
  The `--global` user path is the distinguishing capability, deploying
  skills where OpenClaw reads its managed/local skill directory
  (priority 4 in the OpenClaw loading order).

## Skills convergence

By default, every target with a `skills` primitive deploys to `.agents/skills/<name>/SKILL.md` rather than under the target root. This matches the cross-tool agent skills convention so a single skill bundle serves every harness.

To restore the pre-convergence per-target layout (skills land under each target's own root), use the `--legacy-skill-paths` flag on `apm install` or set `APM_LEGACY_SKILL_PATHS=1`.

## MCP servers

MCP is not a `TargetProfile` primitive; it is wired by a separate
integrator that writes per-client config files (e.g.
`.vscode/mcp.json`, `.cursor/mcp.json`, `.claude.json`, `.kiro/settings/mcp.json`) for every
target in the active set that has an MCP client adapter. Active set
follows the same `--target` > `targets:` > auto-detect chain as
`apm install`: a runtime with an adapter but outside the active set
is skipped and APM emits an `[i] Skipped MCP config for X  (active
targets: Y)` line so the gate decision is observable. The matrix
above marks `mcp` supported when an adapter exists; whether the
config gets written on a given install is a function of the active
target set, not just adapter availability. See
[Install MCP servers](../consumer/install-mcp-servers/) for the
gate behavior and [`apm mcp`](./cli/mcp/) for the runtime surface.

## See also

- [`apm targets`](./cli/targets/) - inspect resolved targets at runtime.
- [`apm compile`](./cli/compile/) - target selection and compile flags.
- [Primitive types](./primitive-types/) - what each primitive is.
- [Primitives and targets](../concepts/primitives-and-targets/) - conceptual model.
