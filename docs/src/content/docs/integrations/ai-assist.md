---
title: "ai-assist Agent (Experimental)"
description: "Deploy APM skills, AGENTS.md instructions, and MCP servers to the ai-assist agent."
sidebar:
  order: 9
---

:::caution[Frontier preview]
This integration is experimental and off by default. You must enable the `ai-assist` flag before using it.

```bash
apm experimental enable ai-assist
```

Until the flag is enabled, the `ai-assist` target stays inert: it is hidden from active target detection, excluded from `apm compile --all`, and explicit `--target ai-assist` installs exit cleanly with an enable hint instead of deploying anything.
:::

## What it does

[ai-assist](https://github.com/ai-assist-org/ai-assist) is a Python AI assistant for knowledge workers, powered by Claude. It integrates with MCP servers for automated monitoring, interactive querying, and report generation. ai-assist natively reads two open standards that APM already emits:

- the [agentskills.io](https://agentskills.io) `SKILL.md` format for skills, and
- the `AGENTS.md` context-file standard for instructions.

So the `ai-assist` target reuses APM's existing skill and `AGENTS.md` output paths and adds one ai-assist-specific writer for MCP servers (ai-assist uses a YAML `servers:` block in a dedicated `mcp_servers.yaml` file, distinct from the JSON `mcpServers` schema of other clients).

| APM primitive | ai-assist surface | Location |
|---------------|-------------------|----------|
| skills | Skills system (agentskills.io) | `.agents/skills/<name>/SKILL.md` (project) or `<config_dir>/skills/<name>/SKILL.md` (`--global`) |
| instructions | Context file (`AGENTS.md`) | `AGENTS.md` at the project root |
| MCP servers | `servers:` block | `<config_dir>/mcp_servers.yaml` (user scope) |

At project scope, skills land in `.agents/skills/`, the cross-client shared directory. At user scope (`--global`), skills land in the ai-assist config directory (default `~/.ai-assist/`).

## Enable the flag

```bash
apm experimental enable ai-assist
apm experimental list
apm experimental disable ai-assist
```

Use `apm experimental list` to confirm whether `ai-assist` is enabled on the current machine.

## Install

```bash
# Project scope: skills -> .agents/skills/, plus AGENTS.md on compile
apm install --target ai-assist

# User scope: skills -> ~/.ai-assist/skills/, MCP servers -> ~/.ai-assist/mcp_servers.yaml
apm install --target ai-assist --global
```

Run your normal `apm compile` flow when you also need `AGENTS.md`; ai-assist shares that standard context-file output.

## AI_ASSIST_CONFIG_DIR override

By default the user-scope root is `~/.ai-assist`. Set `AI_ASSIST_CONFIG_DIR` to point APM at a different config directory (useful for containers and multi-profile setups):

```bash
export AI_ASSIST_CONFIG_DIR="$HOME/.config/ai-assist"
apm install --target ai-assist --global
```

When `AI_ASSIST_CONFIG_DIR` lives under `$HOME`, APM keeps the deploy root home-relative; otherwise it uses the absolute path. The directory does not need to exist yet.

## MCP servers

When the flag is enabled and ai-assist is present (its config directory exists, or the `ai-assist` binary is on `PATH`), APM writes MCP servers into the `servers:` block of `<config_dir>/mcp_servers.yaml`:

```yaml
servers:
  my-server:
    command: npx
    args: ["-y", "my-mcp-package"]
    env:
      MY_TOKEN: "${MY_TOKEN}"
    enabled: true
```

ai-assist resolves `${VAR}` environment variable placeholders at runtime via `os.path.expandvars`, so APM preserves them rather than writing secrets to disk.

HTTP servers are written with `url` and optional `transport`/`headers` instead of `command`/`args`. ai-assist-specific fields (`readonly_tools`, `pagination`) are preserved when present. APM merges into the existing `servers:` block and preserves any other top-level keys for forward-compatibility. All writes go through APM's YAML helper with `0o600` permissions.

## Skills and instructions

- Skills deploy as `SKILL.md` content, unchanged from the agentskills.io format APM already produces.
- Instructions compile to `AGENTS.md`, which ai-assist can read as a context file.
- Agents, prompts, hooks, and commands are not part of the ai-assist surface and are skipped for this target.

## Troubleshooting

- `The 'ai-assist' target requires an experimental flag`: run `apm experimental enable ai-assist`.
- MCP servers not written: confirm the flag is enabled and that `~/.ai-assist/` exists (or `ai-assist` is on `PATH`). APM intentionally skips MCP writes on hosts where ai-assist is absent.
- Wrong config directory: set `AI_ASSIST_CONFIG_DIR` to the directory you want to target.

See also [IDE and Tool Integration](../ide-tool-integration/) and [apm experimental](../../reference/experimental/).
