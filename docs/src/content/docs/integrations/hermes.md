---
title: "Hermes Agent"
description: "Deploy APM skills, AGENTS.md instructions, and MCP servers to the Hermes autonomous agent."
sidebar:
  order: 8
---

Hermes is a stable explicit-only target. Select it with `--target hermes`; it is
not included in `--target all` because its project skills use the shared
`.agents/` root and cannot be safely auto-detected.

## What it does

[Hermes](https://hermes-agent.nousresearch.com) (by Nous Research) is a terminal-native autonomous agent that lives in a home directory (`~/.hermes/`) and talks to users over messaging platforms such as Telegram and Discord. Hermes natively reads two open standards that APM already emits:

- the [agentskills.io](https://agentskills.io) `SKILL.md` format for skills, and
- the `AGENTS.md` context-file standard for instructions.

So the `hermes` target reuses APM's existing skill and `AGENTS.md` output paths and adds one Hermes-specific writer for MCP servers (Hermes uses a YAML `mcp_servers:` block, distinct from the JSON `mcpServers` schema of other clients).

| APM primitive | Hermes surface | Location |
|---------------|----------------|----------|
| skills | Skills system (agentskills.io) | `.agents/skills/<name>/SKILL.md` (project) or `~/.hermes/skills/<name>/SKILL.md` (`--global`) |
| instructions | Context file (`AGENTS.md`) | `AGENTS.md` at the project root |
| MCP servers | `mcp_servers:` block | `~/.hermes/config.yaml` (home-scoped for every explicit selection) |

At project scope, skills land in `.agents/skills/`, which Hermes reads through its `skills.external_dirs` setting. At user scope (`--global`), skills land directly in the Hermes home.

## Install

```bash
# Project scope: skills -> .agents/skills/, MCP -> ~/.hermes/config.yaml
apm install --target hermes

# User scope: skills -> ~/.hermes/skills/, MCP -> ~/.hermes/config.yaml
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

When `hermes` is selected explicitly, APM writes MCP servers into the
home-scoped `mcp_servers:` block of `$HERMES_HOME/config.yaml` (default
`~/.hermes/config.yaml`), even when package skills use project scope:

Explicit selection does not require an existing Hermes home or a `hermes`
binary on `PATH`; APM creates the configured home as needed. Runtime-presence
signals are only relevant to automatic discovery, and Hermes is never
auto-discovered.

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

- MCP servers not written: pass `--target hermes`; Hermes is never selected by automatic runtime discovery.
- Skills not picked up at project scope: ensure Hermes' `skills.external_dirs` includes `.agents/skills/`.
- Wrong home directory: set `HERMES_HOME` to the Hermes home you want to target.

See also [IDE and Tool Integration](../ide-tool-integration/).
