---
title: "Install MCP servers"
description: "Declare MCP servers in apm.yml and let apm install wire them into every detected harness."
---

`apm install` is the same driver for two artifact kinds: APM packages
(see [Install Packages](../install-packages/)) and MCP servers. This
page covers MCP servers: how you declare them, what gets written to
each runtime, and how tokens get injected.

## One-line answer

```bash
apm install --mcp io.github.github/github-mcp-server
```

This adds one entry under `dependencies.mcp:` in `apm.yml` and writes
a runtime-specific MCP config file for every detected harness.

## The `mcp:` section in apm.yml

MCP servers live under `dependencies.mcp:` (or
`devDependencies.mcp:`). Three forms are valid -- pick the one that
matches the source you have:

```yaml
dependencies:
  mcp:
    # 1. Registry reference (bare string)
    - io.github.github/github-mcp-server

    # 2. Self-defined stdio (local process)
    - name: filesystem
      registry: false
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]

    # 3. Self-defined remote (HTTP / SSE)
    - name: linear
      registry: false
      transport: http
      url: https://mcp.linear.app/sse
      headers:
        Authorization: "Bearer ${LINEAR_TOKEN}"
```

The full grammar (overlays, `${input:...}` variables, `tools:`
allowlists, `package:` selection) is in
[Package Anatomy](../../concepts/package-anatomy/).

## Adding a server from the CLI

`apm install --mcp NAME` writes the entry into `apm.yml` for you,
then runs install. Three shapes match the three manifest forms:

```bash
# Registry
apm install --mcp io.github.github/github-mcp-server

# stdio (everything after `--` is the spawn command)
apm install --mcp filesystem -- npx -y @modelcontextprotocol/server-filesystem /workspace

# Remote
apm install --mcp linear --transport http --url https://mcp.linear.app/sse
```

`apm mcp install NAME ...` is an alias that forwards to the same code
path. The `apm mcp` group also provides `search`, `list`, and `show`
for discovery -- see the [CLI reference](../../reference/cli/install/).

## What `apm install` writes to disk

For every harness APM detects in your environment, `apm install`
writes a runtime-specific MCP config file. The schemas differ; the
`apm.yml` source of truth does not.

| Harness | File | Scope | Format |
|---|---|---|---|
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` | global | JSON `mcpServers` |
| VS Code (Copilot) | `.vscode/mcp.json` | project | JSON `servers` |
| Claude Code | `.mcp.json` (project) or `~/.claude.json` (`-g`) | both | JSON `mcpServers` |
| Cursor | `.cursor/mcp.json` | project (only if `.cursor/` exists) | JSON `mcpServers` |
| Codex CLI | `~/.codex/config.toml` | global | TOML `[mcp_servers.*]` |
| Gemini CLI | `.gemini/settings.json` (project, only if `.gemini/` exists) or `~/.gemini/settings.json` (`-g`) | both | JSON `mcpServers` |
| OpenCode | `opencode.json` | project (only if `.opencode/` exists) | JSON `mcp` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | global | JSON `mcpServers` |

Cursor, Gemini, and OpenCode are opt-in by directory in project scope:
APM only writes their config when the corresponding `.cursor/`,
`.gemini/`, or `.opencode/` directory already exists in the project.
This avoids creating runtime artifacts for tools you do not use.
Gemini's user-scope path (`~/.gemini/settings.json`, selected with
`-g`) is unconditional and creates `~/.gemini/` if needed.

`apm install -g --mcp NAME` routes the write to each runtime's
user-scope MCP config when that runtime supports user scope -- for
example Copilot CLI writes to `~/.copilot/mcp-config.json`, Codex
CLI to `~/.codex/config.toml`, and Gemini CLI to
`~/.gemini/settings.json`. Workspace-only runtimes (VS Code, Cursor,
OpenCode) are skipped at user scope.

## stdio vs HTTP servers

MCP defines two transport families. APM exposes both:

- **stdio** -- APM (and your harness) spawns a local process and
  speaks MCP over its stdio. Requires `command:` and optional
  `args:`. Use `--env KEY=VALUE` (repeatable) for environment
  variables. Servers do not go through a shell, so `$VAR` and
  backticks in `args` are passed literally.
- **http / sse / streamable-http** -- APM points your harness at a
  remote endpoint. Requires `url:` (http or https only -- websockets
  and `file://` are rejected). Use `--header KEY=VALUE` (repeatable)
  for HTTP headers such as `Authorization`.

`--transport` is inferred when omitted: a `--url` implies a remote
transport, a post-`--` command implies `stdio`. The mutually-exclusive
combinations (`--url` plus stdio command, `--header` without `--url`,
etc.) are rejected with exit code 2.

## Token injection: GitHub MCP server

APM does not template arbitrary environment variables into MCP config
files (your harness does that at runtime). It does inject one
specific credential automatically:

When the Copilot CLI adapter writes a remote MCP config and the
server is identified as the GitHub MCP server, APM resolves a token
and adds an `Authorization: Bearer <token>` header.

The server is identified as "GitHub" by **two** narrow checks
([copilot.py:1208](https://github.com/microsoft/apm/blob/main/src/apm_cli/adapters/client/copilot.py#L1208)):

1. The server name (case-insensitive) is one of:
   `github-mcp-server`, `github`, `github-mcp`,
   `github-copilot-mcp-server`.
2. Or the parsed URL hostname matches the GitHub host allowlist
   (`github.com`, `api.github.com`, and registered GHES hostnames).

This is an exact-match allowlist on hostname, not a substring check.
A URL like `https://github.com.evil.example` does not match because
the parsed hostname is `github.com.evil.example`, not `github.com`.

The token is resolved from this chain (first non-empty wins):

1. `GITHUB_COPILOT_PAT`
2. `GITHUB_TOKEN`
3. `GITHUB_APM_PAT`
4. `GITHUB_PERSONAL_ACCESS_TOKEN` (Copilot CLI compat)

If none are set, no header is injected and the server is written
without auth -- you will get an unauthenticated request at runtime.
For other authenticated remote servers, set headers explicitly with
`--header Authorization="Bearer ${MY_TOKEN}"`.

## Updating and replacing a server

Re-run `apm install --mcp NAME ...` against an existing entry:

| Situation | Behaviour |
|---|---|
| New `NAME` | Appended to `dependencies.mcp`. |
| Existing `NAME`, identical config | No-op. Logs `unchanged`. |
| Existing `NAME`, different config, TTY | Prompts to replace. |
| Existing `NAME`, different config, CI | Refuses with exit 2. Re-run with `--force`. |

Use `--dry-run` to preview the manifest change without writing.

## Sibling commands

The `apm mcp` group is for discovery and standalone install:

```
apm mcp search <query>    # search the configured registry
apm mcp list              # list available servers
apm mcp show <name>       # detailed server info
apm mcp install <name>    # alias for `apm install --mcp <name>`
```

Full flag tables and exit codes: [CLI reference](../../reference/cli/install/).

## Next

- Authoring an MCP server as a primitive of your own package -- see
  the producer ramp.
- Lockfile and trust boundary for transitive MCP servers --
  [Lifecycle](../../concepts/lifecycle/).
