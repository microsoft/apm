---
title: "IDE & tool integration"
description: "How APM deploys primitives into VS Code, Claude Code, Cursor, Codex, Gemini, Antigravity, OpenCode, Windsurf, JetBrains and other AI coding clients."
sidebar:
  order: 3
---

APM ships agent context (instructions, prompts, agents, skills, MCP servers) into the directories your AI coding tools read at runtime. Each tool has its own slot layout; APM detects which slots exist and writes the right files in the right places.

This page is a hub. It tells you which tools are supported, how detection works, and where to read the per-tool details.

## Supported tools

The full slot-by-slot capability table lives in [Targets matrix](../reference/targets-matrix/). At a glance, APM currently writes for:

| Target               | Marker / signal                     | Notes                                  |
|----------------------|--------------------------------------|----------------------------------------|
| VS Code + Copilot    | `.github/copilot-instructions.md`    | Native instructions, prompts, agents   |
| Claude Code          | `.claude/`                           | Skills, agents, commands, MCP          |
| Cursor               | `.cursor/`                           | Rules, commands, MCP                   |
| Codex CLI            | `.codex/`                            | Skills, MCP                            |
| Gemini CLI           | `.gemini/` or `GEMINI.md`            | Single-file or distributed             |
| Antigravity CLI      | explicit `--target antigravity`       | Rules, skills, hooks, MCP              |
| OpenCode             | `.opencode/`                         | Skills, MCP                            |
| Windsurf             | `.windsurf/`                         | Rules + Skills + Workflows + MCP       |
| Kiro                 | `.kiro/`                             | Steering + Skills + Hooks + MCP        |
| JetBrains Copilot    | user-scope config dir (global)       | MCP only (user-scope path, `${env:VAR}` env substitution) |
| Agent-Skills (cross) | `.agents/skills/`                    | Vendor-neutral skill sharing           |

For exact per-target capabilities (which primitives are supported, transformer used, file layout), see [Targets matrix](../reference/targets-matrix/).

## How target detection works

When you run `apm install` or `apm compile` without `--target`, APM auto-detects tools with project markers above. Explicit-only targets such as Antigravity and `agent-skills` must be selected with `--target`.

```bash
apm targets                    # list detected and supported targets
apm install --target claude    # force a specific target
```

If no marker is present, APM emits the `[x] No harness detected` error - see [Common errors](../troubleshooting/common-errors/).

To pin targets in the manifest:

```yaml
# apm.yml
target:
  - claude
  - copilot
  - cursor
```

The `target:` field accepts either a YAML list or a CSV string. See [Manifest schema](../reference/manifest-schema/#target).

## Primitive flow per target

Each primitive type maps to a target-specific slot:

```
.apm/instructions/   ->   per target: rules / instructions / system prompts
.apm/prompts/        ->   per target: prompt files / commands
.apm/agents/         ->   per target: agent definitions (or skill conversion)
.apm/skills/         ->   per target: skills directory (Claude, Codex, OpenCode, .agents)
.apm/hooks/          ->   per target: lifecycle hooks / tool hooks (varies by target)
mcp: in apm.yml      ->   per target: .mcp.json / settings.json / equivalent
```

Not every target supports every primitive type. When a primitive can't land on a target, APM emits a warning at install time. Skim [Targets matrix](../reference/targets-matrix/) to set expectations before adding a primitive.

> **Deduplication**: When `.github/instructions/` already contains `.instructions.md` files (deployed by `apm install --target copilot`), `apm compile --target copilot` omits `AGENTS.md` entirely when its only content would be the duplicated instructions section. When `.claude/rules/` already contains `.md` files (deployed by `apm install --target claude`), `apm compile --target claude` omits the instructions section from `CLAUDE.md` for the same reason. The context file is still generated when it carries non-instruction content such as a constitution. See [Copilot deduplication](../producer/compile/#copilot-deduplication) for details.

## Common workflows

### Add a target to an existing project

```bash
# Add Cursor alongside an existing Copilot setup
mkdir .cursor
apm install            # auto-detects the new marker
apm compile            # writes Cursor-specific output
```

Or pin in `apm.yml` and rerun install.

### Remove a target

1. Edit `apm.yml` to drop the target from `target:`.
2. `apm prune` to remove APM-managed files for the dropped target.
3. `apm install && apm compile` to verify.

See [Migration paths -> target migration](../troubleshooting/migration/#5-target-migration).

### Cross-tool sharing via .agents/skills

For team projects where contributors use different IDEs, the `agent-skills` target writes a vendor-neutral `.agents/skills/` tree that Claude Code, Codex, OpenCode, and others read directly. This avoids per-tool duplication when your team is multi-vendor.

```bash
apm install --target agent-skills
```

## MCP server integration

MCP servers declared in `apm.yml` (under `dependencies.mcp:` or `devDependencies.mcp:`) are wired into each target's MCP config on install:

- `.mcp.json` at the repo root when `.claude/` exists (Claude Code project scope)
- `.cursor/mcp.json` (Cursor)
- `.codex/config.toml` (Codex)
- `.vscode/mcp.json` (VS Code)
- `opencode.json` at the repo root when `.opencode/` exists (OpenCode)
- `.gemini/settings.json` (Gemini)
- `~/.codeium/windsurf/mcp_config.json` (Windsurf)
- `.kiro/settings/mcp.json` and `~/.kiro/settings/mcp.json` (Kiro IDE)
- OS-specific `github-copilot/intellij/mcp.json` (JetBrains Copilot -- uses
  `"servers"` key, user-scope global path):
  - `%LOCALAPPDATA%\github-copilot\intellij\mcp.json` (Windows)
  - `~/Library/Application Support/github-copilot/intellij/mcp.json` (macOS)
  - `~/.local/share/github-copilot/intellij/mcp.json` (Linux, honouring `XDG_DATA_HOME`)

For server installation patterns, registry resolution, and trust model, see [MCP servers guide](../consumer/install-mcp-servers/) and [`apm mcp`](../reference/cli/mcp/).

### Kiro IDE

[Kiro](https://kiro.dev) reads project configuration from `.kiro/`. APM maps
instructions to `.kiro/steering/` and converts `applyTo:` scoping into Kiro
steering frontmatter (`inclusion: fileMatch`); unscoped instructions become
`inclusion: always`. Skills are copied verbatim to `.kiro/skills/`, hooks
become one Kiro v1 JSON file (`version: "v1"` with a `hooks` array) per hook
action in `.kiro/hooks/`, and MCP servers are written to
`.kiro/settings/mcp.json` or `~/.kiro/settings/mcp.json` for `--global`.
APM maps portable events to Kiro's PascalCase triggers and also accepts native
Kiro v1 hook documents for target-specific triggers. Native hook-level
`description`, `timeout`, and `enabled` fields are preserved. Portable
`askAgent` actions become Kiro v1 `agent` actions.

This target covers the documented Kiro IDE layout. Kiro CLI configuration
differences are tracked separately; see [the targets matrix](../reference/targets-matrix/#kiro).

### JetBrains (IntelliJ IDEA, PyCharm, GoLand, and others)

GitHub Copilot for JetBrains reads MCP servers from a single user-scope
`mcp.json` (the per-OS path above), so configuration is global rather than
per-project. Prerequisite: install the GitHub Copilot plugin in your JetBrains
IDE at least once so the `github-copilot/intellij/` config directory exists --
that directory is the auto-detect signal.

```bash
# Install an MCP server into the JetBrains user-scope config
apm install --mcp --runtime intellij <package>
```

Notes and limits:

- **Auto-detect is user-scope only.** Unlike project markers such as `.cursor/`
  or `.windsurf/`, JetBrains is detected from the global config directory, not a
  file in your repo. It is therefore detected for every project on the machine
  once the plugin directory exists. Use `--runtime intellij` to target it
  explicitly regardless of auto-detect.
- **Runtime env substitution.** JetBrains Copilot resolves `${env:VAR}` in
  `mcp.json` at server start. APM preserves env-var placeholders as
  `${env:VAR}` instead of writing matching host secrets into the config.

## Per-tool reference pages

Pinpoint behaviour, slot layout, and known limits per target:

- [Targets matrix](../reference/targets-matrix/) - capability grid
- [`apm targets`](../reference/cli/targets/) - detection and listing
- [`apm install`](../reference/cli/install/) - target selection flags
- [`apm compile`](../reference/cli/compile/) - per-target output
- [`apm mcp`](../reference/cli/mcp/) - MCP wiring per target

## Troubleshooting

| Symptom                                       | Where to look                                                              |
|-----------------------------------------------|----------------------------------------------------------------------------|
| `[x] No harness detected`                     | [Common errors](../troubleshooting/common-errors/)                          |
| Compile produced no output                    | [Compile zero-output](../troubleshooting/compile-zero-output-warning/)      |
| Wrong target picked, multiple harnesses       | [`apm targets`](../reference/cli/targets/)                                  |
| MCP server not appearing in tool              | [MCP servers guide](../consumer/install-mcp-servers/)                       |
| Cursor command file dropped                   | [Targets matrix](../reference/targets-matrix/) - `claude_command` transformer |

## Related resources

- [Targets matrix](../reference/targets-matrix/)
- [Manifest schema](../reference/manifest-schema/)
- [MCP servers](../consumer/install-mcp-servers/)
- [GitHub Agentic Workflows](./gh-aw/)
- [Microsoft 365 Copilot Cowork](./copilot-cowork/)
- [APM in CI/CD](./ci-cd/)
- [Runtime compatibility](./runtime-compatibility/)
