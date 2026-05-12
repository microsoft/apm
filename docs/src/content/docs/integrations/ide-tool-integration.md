---
title: "IDE & tool integration"
description: "How APM deploys primitives into VS Code, Claude Code, Cursor, Codex, Gemini, OpenCode, Windsurf and other AI coding clients."
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
| OpenCode             | `.opencode/`                         | Skills, MCP                            |
| Windsurf             | `.windsurf/`                         | Rules + MCP (lossy agent->skill)       |
| Agent-Skills (cross) | `.agents/skills/`                    | Vendor-neutral skill sharing           |

For exact per-target capabilities (which primitives are supported, transformer used, file layout), see [Targets matrix](../reference/targets-matrix/).

## How target detection works

When you run `apm install` or `apm compile` without `--target`, APM auto-detects which tools your project uses by looking for the markers above. Multiple targets can be active simultaneously.

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
.apm/hooks/          ->   per target: lifecycle hooks (Claude only today)
mcp: in apm.yml      ->   per target: .mcp.json / settings.json / equivalent
```

Not every target supports every primitive type. When a primitive can't land on a target, APM emits a warning at install time. Skim [Targets matrix](../reference/targets-matrix/) to set expectations before adding a primitive.

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

MCP servers declared in `apm.yml` (under `mcp:`) are wired into each target's MCP config on install:

- `.claude/settings.json` (Claude Code)
- `.cursor/mcp.json` (Cursor)
- `.codex/mcp.json` (Codex)
- `.vscode/mcp.json` (VS Code)
- `.opencode/mcp.json` (OpenCode)
- `.windsurf/mcp.json` (Windsurf)

For server installation patterns, registry resolution, and trust model, see [MCP servers guide](../guides/install-and-use/mcp-servers/) and [`apm mcp`](../reference/cli/mcp/).

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
| MCP server not appearing in tool              | [MCP servers guide](../guides/install-and-use/mcp-servers/)                 |
| Cursor command file dropped                   | [Targets matrix](../reference/targets-matrix/) - `claude_command` transformer |

## Related resources

- [Targets matrix](../reference/targets-matrix/)
- [Manifest schema](../reference/manifest-schema/)
- [MCP servers](../guides/install-and-use/mcp-servers/)
- [GitHub Agentic Workflows](./gh-aw/)
- [Microsoft 365 Copilot Cowork](./copilot-cowork/)
- [APM in CI/CD](./ci-cd/)
- [Runtime compatibility](./runtime-compatibility/)
