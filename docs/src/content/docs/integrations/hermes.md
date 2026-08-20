---
title: "Hermes Agent (Experimental)"
description: "Deploy APM skills, AGENTS.md instructions, and MCP servers to the Hermes autonomous agent."
sidebar:
  order: 8
---

:::caution[Frontier preview]
This integration is experimental and off by default. You must enable the `hermes` flag before using it.

```bash
apm experimental enable hermes
```

Until the flag is enabled, the `hermes` target stays inert: it is hidden from active target detection, excluded from `apm compile --all`, and explicit `--target hermes` installs exit cleanly with an enable hint instead of deploying anything.
:::

## What it does

[Hermes](https://hermes-agent.nousresearch.com) (by Nous Research) is a terminal-native autonomous agent that lives in a home directory (`~/.hermes/`) and talks to users over messaging platforms such as Telegram and Discord. Hermes natively reads two open standards that APM already emits:

- the [agentskills.io](https://agentskills.io) `SKILL.md` format for skills, and
- the `AGENTS.md` context-file standard for instructions.

So the `hermes` target reuses APM's existing skill and `AGENTS.md` output paths and adds one Hermes-specific writer for MCP servers (Hermes uses a YAML `mcp_servers:` block, distinct from the JSON `mcpServers` schema of other clients).

| APM primitive | Hermes surface | Location |
|---------------|----------------|----------|
| skills | Skills system (agentskills.io) | `.agents/skills/<name>/SKILL.md` (project) or `~/.hermes/skills/<name>/SKILL.md` (`--global`) |
| instructions | Context file (`AGENTS.md`) | `AGENTS.md` at the project root |
| MCP servers | `mcp_servers:` block | `~/.hermes/config.yaml` (user scope) |

At project scope, skills land in `.agents/skills/`, which Hermes reads through its `skills.external_dirs` setting. At user scope (`--global`), skills land directly in the Hermes home.

## Enable the flag

```bash
apm experimental enable hermes
apm experimental list
apm experimental disable hermes
```

Use `apm experimental list` to confirm whether `hermes` is enabled on the current machine.

## Install

```bash
# Project scope: skills -> .agents/skills/, plus AGENTS.md on compile
apm install --target hermes

# User scope: skills -> ~/.hermes/skills/, MCP servers -> ~/.hermes/config.yaml
apm install --target hermes --global
```

Run your normal `apm compile` flow when you also need `AGENTS.md`; Hermes shares that standard context-file output.

## HERMES_HOME override

By default the user-scope root is `~/.hermes`. Set `HERMES_HOME` to point APM at a different Hermes home (useful for containers and multi-profile setups):

```bash
export HERMES_HOME="$HOME/.config/hermes"
apm install --target hermes --global
```

When `HERMES_HOME` lives under `$HOME`, APM keeps the deploy root home-relative; otherwise it uses the absolute path. The directory does not need to exist yet.

## MCP servers

When the flag is enabled and Hermes is present (its home directory exists, or the `hermes` binary is on `PATH`), APM writes MCP servers into the `mcp_servers:` block of `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  my-server:
    command: npx
    args: ["-y", "my-mcp-package"]
    env:
      MY_TOKEN: "..."
    enabled: true
```

HTTP servers are written with `url` and optional `headers` instead of `command`/`args`. APM merges into the existing `mcp_servers:` block and preserves every other top-level key in `config.yaml` (model provider, platform settings, and so on). All writes go through APM's YAML helper, so existing comments outside the managed block are the only thing not preserved by a safe-dump rewrite.

## Skills and instructions

- Skills deploy as `SKILL.md` content, unchanged from the agentskills.io format APM already produces.
- Instructions compile to `AGENTS.md`, which Hermes reads as a first-class context file.
- Agents, prompts, hooks, and commands are not part of the Hermes surface and are skipped for this target.

## Troubleshooting

- `The 'hermes' target requires an experimental flag`: run `apm experimental enable hermes`.
- MCP servers not written: confirm the flag is enabled and that `~/.hermes/` exists (or `hermes` is on `PATH`). APM intentionally skips MCP writes on hosts where Hermes is absent.
- Skills not picked up at project scope: ensure Hermes' `skills.external_dirs` includes `.agents/skills/`.
- Wrong home directory: set `HERMES_HOME` to the Hermes home you want to target.

See also [IDE and Tool Integration](../ide-tool-integration/) and [apm experimental](../../reference/experimental/).
