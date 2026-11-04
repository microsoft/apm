---
title: MCP variable compatibility
description: How each supported harness reads environment references in MCP configuration, what APM writes for it, and the gaps that remain.
sidebar:
  order: 7
---

`apm.yml` can reference environment variables in MCP `env`, `args`, `command`,
`url` and `headers` (see [Variable references](../manifest-schema/#424-variable-references-in-headers-and-env)).
Every harness reads references differently, so APM renders them per harness.
This page records, for each supported harness, the native contract, what APM
writes, and the gaps that remain.

The results were checked on 2026-10-24 with the change for
[#3252](https://github.com/microsoft/apm/issues/3252). Evidence is marked as:

- **client**: checked against the native client.
- **docs**: taken from the harness's official documentation only.
- **unverified**: the documentation does not say and no client was available.

A harness that is only documented or unverified is not counted as passing.

## Native contract

| Harness (client checked) | APM writes to | Native syntax and fields | `${VAR:-default}` | Unset, no default | Approval before servers run | Evidence |
|---|---|---|---|---|---|---|
| Claude Code (2.1.310) | `.mcp.json` (project), `~/.claude.json` (user) | `${VAR}` in `command`, `args`, `env`, `url`, `headers` | Supported | Left as literal text; `claude mcp list` warns | Project servers need workspace trust and approval; user servers do not | client |
| GitHub Copilot CLI (1.0.44) | `.github/mcp.json` (project), `~/.copilot/mcp-config.json` (user) | `${NAME}` in `command`, `args`, `env`, header values (portable format) | Supported (portable format) | Not documented | Project servers load only after folder trust | docs; client for file locations |
| VS Code | `.vscode/mcp.json` (project) | `${env:NAME}`, `${input:id}` in `env`, `envFile`, `cwd`, `headers` | Only in the portable format | Not documented | Workspace servers follow Workspace Trust | docs |
| Cursor | `.cursor/mcp.json` (project), `~/.cursor/mcp.json` (user) | `${env:NAME}` in `command`, `args`, `env`, `url`, `headers` | Not documented | Not documented | Tool calls ask for approval by default | docs |
| Codex CLI | `.codex/config.toml` (project), `~/.codex/config.toml` (user) | No interpolation. `env_vars` forwards variables; `bearer_token_env_var` and `env_http_headers` name variables for HTTP | Not applicable | Not applicable | Project config only in trusted projects | docs |
| Gemini CLI | `.gemini/settings.json` (project), `~/.gemini/settings.json` (user) | `$VAR` or `${VAR}` in `env` | Not documented | Empty string | stdio servers connect only in trusted folders | docs |
| Antigravity | `.agents/mcp_config.json` (project), `~/.gemini/config/mcp_config.json` (user) | No interpolation documented; `env` is literal | Not documented | Not documented | Tool calls run in Ask mode by default | docs |
| Hermes Agent | `~/.hermes/config.yaml` (user) | `${VAR}` documented for catalog entries (`command`, `args`, `url`, `headers`); `config.yaml` entries not documented | Not documented | Not documented | `enabled: false` skips a server | unverified |
| OpenCode | `opencode.json` (project), `~/.config/opencode/opencode.json` (user) | `{env:VAR}` in config values | Not documented | Empty string | Not documented | docs |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` (user) | `${env:VAR}` in `command`, `args`, `env`, `serverUrl`, `url`, `headers` | Not documented | Empty string | Team allowlists only | docs (Cascade documentation) |
| Kiro | `.kiro/settings/mcp.json` (project), `~/.kiro/settings/mcp.json` (user) | `${VAR}` (documented in `env` and `headers`) | Not documented | Not documented | Only variables listed in "Mcp Approved Env Vars" are expanded | docs |
| JetBrains Copilot | `$XDG_CONFIG_HOME/github-copilot/intellij/mcp.json` (user) | Not documented | Not documented | Not documented | Not documented | unverified |

## What APM writes

For one self-defined stdio server and one self-defined HTTP server that use a
set variable (`${DEMO_TOKEN}`), a defaulted one (`${DEMO_UNSET:-fallback}`),
an unset one (`${DEMO_MISSING}`) and a static value (`debug`):

| Harness, scope | Set reference | Defaulted reference | Static value | Value written to the file? |
|---|---|---|---|---|
| Claude Code, project | `${DEMO_TOKEN}` in every field | Kept | Kept | No |
| Claude Code, user | `env`, `headers`: resolved at install time (unchanged); `command`, `args`, `url` copied | Kept | Kept | Yes (file is `0o600`, outside the repo) |
| Copilot CLI, project and user | `${DEMO_TOKEN}` | `env`: replaced by `${KEY}`; other fields kept | `env`: replaced by `${KEY}` | No |
| VS Code, project | `env`, `headers`: `${env:DEMO_TOKEN}`; `args`: bare `${DEMO_TOKEN}` | Kept | Kept | No |
| Cursor, project and user | `${env:DEMO_TOKEN}` in `env`, `args`, `headers`; `command` and `url` copied | Kept | Kept | No |
| Codex CLI, project and user | `env`: resolved; `Authorization`: `bearer_token_env_var` | Kept | Kept | Yes |
| Gemini CLI, project and user | Resolved at install time | Kept | Kept | Yes |
| Antigravity, project and user | Resolved at install time | Kept | Kept | Yes |
| Hermes Agent, user | Resolved at install time | Kept | Kept | Yes |
| OpenCode, project and user | `{env:DEMO_TOKEN}` in `env`, `command`, `headers`; `url` copied | Kept as `${...}` | Kept | No |
| Windsurf, user | Resolved at install time | Kept | Kept | Yes |
| Kiro, project and user | `${DEMO_TOKEN}` | `env`: replaced by `${KEY}` | `env`: replaced by `${KEY}` | No |
| JetBrains Copilot, user | `${env:DEMO_TOKEN}` | `env`: replaced by `${env:KEY}` | `env`: replaced by `${env:KEY}` | No |

## Known gaps

These existed before #3252 and are recorded here rather than changed by it:

- **Codex CLI, Gemini CLI, Antigravity, Hermes Agent and Windsurf** write the
  resolved value into their configuration.
- **Claude Code user scope** resolves at install time although Claude Code
  expands references in `~/.claude.json` too.
- **Targets that resolve at install time** (Claude Code user scope, Codex CLI,
  Gemini CLI, Antigravity, Hermes Agent, Windsurf) resolve `env` and `headers`
  but copy references in `command`, `args` and `url` unchanged, including
  spellings such as `${env:VAR}` that the harness may not read.
- **Cursor and OpenCode** copy references in `url` (and Cursor in `command`)
  unchanged.
- **Copilot CLI, Kiro and JetBrains Copilot** replace static `env` values and
  defaulted `env` references with a reference named after the key.
- **VS Code** leaves bare `${VAR}` in `args`, which the VS Code format does not
  expand.
- **Cursor, VS Code, OpenCode, Codex, Gemini CLI, Antigravity, Hermes Agent and
  Windsurf** copy `${VAR:-default}` unchanged, although their documented syntax
  has no defaults.
- **Copilot CLI 1.0.44** reads project servers from `.mcp.json` only, not the
  `.github/mcp.json` that APM writes. Current GitHub documentation lists both.
- **Every harness**: an MCP `url` must start with a literal `http://` or
  `https://`, so the scheme itself cannot be a reference.

## How this was checked

- APM's output: the same declarations were installed for every harness and
  scope with the installed CLI in an isolated home, using synthetic values,
  and each generated file and the CLI output were searched for those values.
- Claude Code: a project `.mcp.json` and a user `~/.claude.json` using
  `${VAR}`, `${VAR:-default}` and an unset variable in every field were loaded
  by Claude Code 2.1.310 in an isolated configuration directory. A recording
  stdio command and a local HTTP endpoint captured the expanded values.
- Copilot CLI: `copilot mcp list` (1.0.44) in an isolated home showed which
  project files it reads.
