---
title: "OpenClaw Agent (Experimental)"
description: "Deploy APM skills to the OpenClaw autonomous agent runtime."
sidebar:
  order: 9
---

:::caution[Frontier preview]
This integration is experimental and off by default. You must enable the `openclaw` flag before using it.

```bash
apm experimental enable openclaw
```

Until the flag is enabled, the `openclaw` target stays inert: it is hidden from active target detection, excluded from `apm compile --all`, and explicit `--target openclaw` installs exit cleanly with an enable hint instead of deploying anything.
:::

## What it does

[OpenClaw](https://github.com/openclaw/openclaw) is an autonomous agent runtime that natively reads the [agentskills.io](https://agentskills.io) `SKILL.md` format -- the same format APM already emits for skills. The `openclaw` target deploys skills to directories that OpenClaw scans at startup.

At project scope, skills land in `.agents/skills/` (identical to the `agent-skills` target). The distinguishing capability is user scope (`--global`), which deploys skills to `~/.openclaw/skills/` -- OpenClaw's managed skill directory (priority 4 in the OpenClaw loading order).

| APM primitive | OpenClaw surface | Location |
|---------------|------------------|----------|
| skills | Skills system (agentskills.io) | `.agents/skills/<name>/SKILL.md` (project) or `~/.openclaw/skills/<name>/SKILL.md` (`--global`) |

## Enable the flag

```bash
apm experimental enable openclaw
apm experimental list
apm experimental disable openclaw
```

Use `apm experimental list` to confirm whether `openclaw` is enabled on the current machine.

## Install

```bash
# Project scope: skills -> .agents/skills/
apm install --target openclaw

# User scope: skills -> ~/.openclaw/skills/
apm install --target openclaw --global
```

OpenClaw can also be combined with other targets in a single install:

```bash
apm install --target openclaw,claude
```

## Supported primitives

- **Skills** deploy as `SKILL.md` content, unchanged from the agentskills.io format APM already produces.
- Agents, instructions, prompts, hooks, commands, and MCP servers are not part of the OpenClaw surface and are skipped for this target.

## Troubleshooting

- `The 'openclaw' target requires an experimental flag`: run `apm experimental enable openclaw`.
- Skills not picked up at project scope: ensure OpenClaw's skill search paths include `.agents/skills/`.
- Skills not picked up at user scope: verify that `~/.openclaw/skills/` exists and that OpenClaw is configured to scan its managed skill directory.

See also [IDE and Tool Integration](../ide-tool-integration/) and [apm experimental](../../reference/experimental/).
