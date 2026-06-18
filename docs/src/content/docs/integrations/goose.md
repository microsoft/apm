---
title: "Goose (Experimental)"
description: "Configure MCP servers and AGENTS.md-backed instructions for the Goose agent by Block."
sidebar:
  order: 10
---

:::caution[Frontier preview]
This integration is experimental and off by default. You must enable the `goose` flag before using it.

```bash
apm experimental enable goose
```

Until the flag is enabled, the `goose` target stays inert: it is hidden from active target detection, excluded from `apm compile --all`, and explicit `--target goose` installs exit cleanly with an enable hint instead of deploying anything.
:::

## What it does

[Goose](https://goose-docs.ai) (by Block) is an on-machine AI agent with a CLI and desktop app. It has no project-level config directory: instruction context comes from a `.goosehints` file read from the project tree, and MCP servers (which Goose calls **extensions**) live only in a single home config at `~/.config/goose/config.yaml`.

The `goose` target maps APM onto Goose's native surfaces:

| APM primitive | Goose surface | Location |
|---------------|---------------|----------|
| agents | Recipe (`goose run --recipe`) | `.goose/recipes/<name>.yaml` (project scope) |
| skills | Skills (agentskills.io `SKILL.md`) | `.agents/skills/<name>/` (project) or `~/.agents/skills/<name>/` (`--global`) |
| instructions | `.goosehints` (imports `AGENTS.md`) | `.goosehints` + `AGENTS.md` at the project root |
| MCP servers | `extensions:` block | `~/.config/goose/config.yaml` (user scope) |

Goose's hint files support an `@path` import preprocessor, so APM emits a thin `.goosehints` stub containing `@./AGENTS.md` rather than a second copy of the instruction roll-up — exactly the pattern used for `GEMINI.md`. Prompts, hooks, and commands are not part of the Goose surface and are skipped for this target.

## Enable the flag

```bash
apm experimental enable goose
apm experimental list
apm experimental disable goose
```

Use `apm experimental list` to confirm whether `goose` is enabled on the current machine.

## Install

```bash
# Project scope: recipes -> .goose/recipes/, skills -> .agents/skills/,
# plus AGENTS.md + .goosehints on compile
apm install --target goose
apm compile -t goose

# User scope: skills -> ~/.agents/skills/, MCP servers -> ~/.config/goose/config.yaml
apm install --target goose --global
```

`apm compile -t goose` emits `AGENTS.md` at the project root (the `goose` target shares the `agents` compile family) plus a `.goosehints` stub that imports it.

## Recipes (from APM agents)

Each APM agent (`.apm/agents/<name>.md`) compiles to a Goose **recipe** at `.goose/recipes/<name>.yaml` — the native packaged-agent unit you run with `goose run --recipe <name>`. The agent's frontmatter and body map directly:

```yaml
version: 1.0.0
title: security-review
description: Reviews diffs for OWASP issues.
instructions: |
  You are a security reviewer. Inspect the working diff for...
settings:
  goose_model: gpt-5   # only when the agent pins `model:`
```

Recipes load from the current directory or `$GOOSE_RECIPE_PATH`, so point Goose at the generated folder when running outside the project root:

```bash
export GOOSE_RECIPE_PATH=.goose/recipes
goose run --recipe security-review
```

MCP `extensions:` are intentionally **not** embedded in recipes: an APM agent declares no MCP servers (those live at package scope and are written to `config.yaml`, which Goose reads globally at run time). Recipes are project-scope only — Goose has no canonical user-scope recipe home.

## Skills

Skills deploy to the cross-tool `.agents/skills/<name>/SKILL.md` standard that Goose reads natively (`.agents/skills/` at project scope, `~/.agents/skills/` with `--global`). No transformation is applied — the `SKILL.md` is the format APM already produces.

## $XDG_CONFIG_HOME override

By default the MCP config is written to `~/.config/goose/config.yaml`. When `XDG_CONFIG_HOME` is set, APM writes to `$XDG_CONFIG_HOME/goose/config.yaml` instead, matching Goose's own resolution:

```bash
export XDG_CONFIG_HOME="$HOME/.config"
apm install --target goose --global
```

## MCP servers

When the flag is enabled, APM writes MCP servers into the `extensions:` block of `~/.config/goose/config.yaml` using Goose's native per-server schema:

```yaml
extensions:
  my-server:
    name: my-server
    type: stdio
    cmd: npx
    args: ["-y", "my-mcp-package"]
    envs:
      MY_TOKEN: "..."
    enabled: true
    timeout: 300
```

Remote servers are written with `type: streamable_http` and a `uri` (plus optional `headers`) instead of `cmd`/`args`. APM merges into the existing `extensions:` block and preserves every other top-level key in `config.yaml` (model provider, UI settings, other extensions, and so on). The file is written atomically with `0o600` permissions because it carries literal credentials; a malformed existing `config.yaml` is left untouched rather than overwritten.

## Instructions

- Instructions compile to `AGENTS.md`, and a `.goosehints` stub at the project root pulls it in via Goose's `@./AGENTS.md` import.
- Place project-specific context directly in your own `.goosehints` only if you want content outside the APM-managed roll-up; APM regenerates the stub on each compile.

## Troubleshooting

- `The 'goose' target requires an experimental flag`: run `apm experimental enable goose`.
- MCP servers not written: confirm the flag is enabled and that you passed `--global` (Goose MCP config is user-scope only).
- `config.yaml is malformed YAML; refusing to overwrite`: fix or remove the file manually, then retry — APM never discards a config it cannot parse.
- Hints not picked up: ensure `.goosehints` and `AGENTS.md` are at the directory where you launch Goose (the project root).

See also [IDE and Tool Integration](../ide-tool-integration/) and [apm experimental](../../reference/experimental/).
