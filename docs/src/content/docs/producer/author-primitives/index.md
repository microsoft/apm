---
title: Author primitives
description: The primitive types you can ship in an APM package and where each maps on each target harness.
sidebar:
  order: 0
---

A package's `.apm/` directory contains primitives. Each primitive type is an opinion about how a piece of agent context should be authored once and projected onto every supported target (Copilot, Claude, Cursor, OpenCode, Codex, Gemini, Windsurf).

## Primitive types

| Primitive       | One-liner                                                                | Page                                              |
|-----------------|--------------------------------------------------------------------------|---------------------------------------------------|
| Skills          | Self-contained capability bundles with `SKILL.md` + scripts + assets     | [Skills](./skills/)                               |
| Prompts         | Reusable prompt templates with frontmatter                               | [Prompts](./prompts/)                             |
| Instructions    | Long-lived behavior rules (style guides, conventions)                    | [Instructions and agents](./instructions-and-agents/) |
| Agents          | Personas with explicit scope, tools, and triggers                        | [Instructions and agents](./instructions-and-agents/) |
| Hooks           | Event handlers fired by the runtime (pre-commit, on-tool-use, ...)       | [Hooks and commands](./hooks-and-commands/)       |
| Commands        | Slash-command shortcuts the developer types into the agent UI            | [Hooks and commands](./hooks-and-commands/)       |
| MCP servers     | Tool-server declarations consumers can wire into their harness           | [MCP as a primitive](./mcp-as-primitive/)         |

## On-disk layout

```text
.apm/
  skills/
    my-skill/
      SKILL.md
      scripts/
      references/
      assets/
  prompts/
    review.prompt.md
  instructions/
    style.instructions.md
  agents/
    cli-logging-expert.agent.md
  hooks/
    pre-commit.hook.md
  commands/
    deploy.command.md
```

Every primitive type follows the same pattern: a markdown file (or directory containing a primary markdown file) with frontmatter declaring its name and its trigger conditions. `apm compile` reads `.apm/`, applies any policy, and writes per-target output to the right directories on the target's filesystem.

## Recommended reading order

1. [Skills](./skills/) -- the densest primitive type and the one most newcomers hit first.
2. [Prompts](./prompts/) -- next-most-common; how reusable templates work.
3. [Instructions and agents](./instructions-and-agents/) -- when behavior should persist across sessions.
4. [Hooks and commands](./hooks-and-commands/) -- runtime extension points.
5. [MCP as a primitive](./mcp-as-primitive/) -- when your package needs to bring along a tool server.

When you're ready to ship, continue to [Compile](../compile/) and [Pack a bundle](../pack-a-bundle/).
