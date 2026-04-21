---
title: "MCP Servers"
description: "Add MCP servers to your project with apm install --mcp. Supports stdio, registry, and remote HTTP servers across Copilot, Claude, Cursor, Codex, and OpenCode."
sidebar:
  order: 6
---

APM manages your agent configuration in `apm.yml` -- think `package.json` for AI. MCP servers are dependencies in that manifest.

`apm install --mcp` adds a server to `apm.yml` and wires it into every detected client (Copilot, Claude, Cursor, Codex, OpenCode) in one step.

## Quick Start

Three shapes cover almost every MCP server you will install. Pick the one that matches what you copied from the server's README.

**stdio (post-`--` argv)** -- most public servers ship as an `npx`/`uvx` invocation:

```bash
apm install --mcp filesystem -- npx -y @modelcontextprotocol/server-filesystem /workspace
```

**Registry (resolved from the MCP registry):**

```bash
apm install --mcp io.github.github/github-mcp-server
```

**Remote (HTTP / SSE):**

```bash
apm install --mcp linear --transport http --url https://mcp.linear.app/sse
```

After any of the three:

```bash
apm mcp list                # confirm server is wired into detected runtimes
```

`apm mcp install` is an alias if you prefer the noun-first form: `apm mcp install filesystem -- npx -y @modelcontextprotocol/server-filesystem /workspace`.

## Three Ways to Add an MCP Server

| Source | Example | When to use |
|--------|---------|-------------|
| stdio command | `apm install --mcp NAME -- <bin> <args...>` | You have a working `npx`/`uvx`/binary invocation from a README. |
| Registry name | `apm install --mcp io.github.github/github-mcp-server` | The server is published to the [MCP registry](https://api.mcp.github.com). Discover with `apm mcp search`. |
| Remote URL | `apm install --mcp NAME --transport http --url https://...` | The server is hosted -- no local process to spawn. |

The post-`--` form is recommended over `--transport stdio` plus separate fields: it is exactly what you can paste from any MCP server's README.

## CLI Reference: `apm install --mcp`

```bash
apm install --mcp NAME [OPTIONS] [-- COMMAND ARGV...]
```

`NAME` is the entry that lands under `dependencies.mcp` in `apm.yml`. It must match `^[a-zA-Z0-9@][a-zA-Z0-9._@/:=-]{0,127}$`.

| Flag | Purpose |
|------|---------|
| `--mcp NAME` | Add `NAME` to `dependencies.mcp` and install it. Required to enter this code path. |
| `--transport stdio\|http\|sse` | Override transport. Inferred from `--url` (remote) or post-`--` argv (stdio) when omitted. |
| `--url URL` | Endpoint for `http` / `sse` transports. Scheme must be `http` or `https`. |
| `--env KEY=VALUE` | Environment variable for stdio servers. Repeatable. |
| `--header KEY=VALUE` | HTTP header for remote servers. Repeatable. Requires `--url`. |
| `--mcp-version VER` | Pin the registry entry to a specific version. |
| `--dev` | Add to `devDependencies.mcp` instead of `dependencies.mcp`. |
| `--force` | Replace an existing entry with the same `NAME` without prompting (CI). |
| `--dry-run` | Print what would be added; do not write `apm.yml` or touch client configs. |
| `-- COMMAND ARGV...` | Everything after `--` is the stdio command for the server. Implies `--transport stdio`. |

`apm mcp install NAME ...` is an alias that forwards to `apm install --mcp NAME ...`.

Inherited flags that still apply: `--runtime`, `--exclude`, `--verbose`. Flags that do **not** apply with `--mcp`: `--global` (MCP entries are workspace-scoped), `--only apm`, `--update`, `--ssh` / `--https` / `--allow-protocol-fallback` -- see [Errors and Conflicts](#errors-and-conflicts).

## What Gets Written

`apm install --mcp` is the interface. `apm.yml` is the result. Each shape produces one of three entry forms.

**stdio command** (`apm install --mcp filesystem -- npx -y @modelcontextprotocol/server-filesystem /workspace`):

```yaml title="apm.yml"
dependencies:
  mcp:
    - name: filesystem
      registry: false
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
```

**Registry reference** (`apm install --mcp io.github.github/github-mcp-server`):

```yaml title="apm.yml"
dependencies:
  mcp:
    - io.github.github/github-mcp-server
```

**Remote** (`apm install --mcp linear --transport http --url https://mcp.linear.app/sse --header Authorization="Bearer $TOKEN"`):

```yaml title="apm.yml"
dependencies:
  mcp:
    - name: linear
      registry: false
      transport: http
      url: https://mcp.linear.app/sse
      headers:
        Authorization: "Bearer $TOKEN"
```

For the full manifest grammar (overlays on registry servers, `${input:...}` variables, package selection), see the [MCP dependencies reference](../dependencies/#mcp-dependency-formats) and the [manifest schema](../../reference/manifest-schema/).

## Updating and Replacing Servers

Re-running `apm install --mcp NAME` against an existing entry is the supported way to change configuration.

| Situation | Behaviour |
|-----------|-----------|
| New `NAME` | Appended to `dependencies.mcp`. Exit 0. |
| Existing `NAME`, identical config | No-op. Logs `unchanged`. Exit 0. |
| Existing `NAME`, different config, interactive TTY | Prints diff, prompts `Replace MCP server 'NAME'?`. Exit 0. |
| Existing `NAME`, different config, non-TTY (CI) | Refuses with exit code 2. Re-run with `--force`. |
| Existing `NAME` + `--force` | Replaces silently. Exit 0. |

Use `--dry-run` to preview the change without writing:

```bash
apm install --mcp filesystem --dry-run -- npx -y @modelcontextprotocol/server-filesystem /new/path
```

## Validation and Security

APM validates every `--mcp` entry before writing `apm.yml`. These are guardrails, not gatekeepers -- they catch the common ways an MCP entry can break a client config or leak credentials.

| Check | Rule | Why |
|-------|------|-----|
| `NAME` shape | `^[a-zA-Z0-9@][a-zA-Z0-9._@/:=-]{0,127}$` | Keeps names round-trippable as YAML keys, file paths, and registry identifiers. |
| `--url` scheme | `http` or `https` only | Blocks `file://`, `gopher://`, and similar exfil vectors. |
| `--header` content | No CR or LF in keys or values | Prevents header injection / response splitting. |
| `command` (stdio) | No path-traversal segments (`..`, absolute escapes) | Blocks an entry from pointing the client at a binary outside the project. |
| Internal / metadata `--url` | Warning, not blocked | Catches accidental cloud-metadata-IP URLs without breaking valid intranet servers. |
| `--env` shell metacharacters | Warning, not blocked | Reminds you that stdio servers do not go through a shell, so `$VAR` and backticks are passed literally. |

Self-defined servers (everything except the bare-string registry form) additionally require:

- `transport` -- one of `stdio`, `http`, `sse`.
- `url` -- when `transport` is `http` or `sse`.
- `command` -- when `transport` is `stdio`.

For the trust boundary on transitive MCP servers (`--trust-transitive-mcp`), see [Dependencies: Trust Model](../dependencies/#mcp-dependency-formats) and [Security Model](../../enterprise/security/).

## Errors and Conflicts

`apm install --mcp` rejects flag combinations that would silently do the wrong thing. All conflicts exit with code 2.

| Error | Trigger | Fix |
|-------|---------|-----|
| `cannot mix --mcp with positional packages` | `apm install owner/repo --mcp foo` | Run `--mcp` and APM-package installs as separate commands. |
| `MCP servers are workspace-scoped; --global not supported` | `apm install -g --mcp foo` | MCP servers always land in the project `apm.yml`. Drop `-g`. |
| `cannot use --only apm with --mcp` | Filtering by APM-only while adding an MCP entry. | Drop `--only apm`. |
| `--header requires --url` | `--header` without an HTTP/SSE endpoint. | Add `--url`, or use `--env` for stdio servers. |
| `cannot specify both --url and a stdio command` | Mixed remote + post-`--` argv. | Pick one shape. |
| `stdio transport doesn't accept --url` | `--transport stdio --url ...` | Use post-`--` argv for stdio. |
| `remote transports don't accept stdio command` | `--transport http -- npx ...` | Drop `--transport http` (or drop the post-`--` argv). |
| `--env applies to stdio MCPs; use --header for remote` | `--env` on a remote server. | Use `--header` for HTTP/SSE auth. |

Existing-entry conflicts (`already exists in apm.yml`) are covered in [Updating and Replacing Servers](#updating-and-replacing-servers).

## Custom registry (enterprise)

`MCP_REGISTRY_URL` overrides the MCP registry endpoint that APM queries. It applies to all `apm mcp` discovery commands (`search`, `list`, `show`) and to `apm install --mcp` when resolving registry-form servers (e.g. `apm install --mcp io.github.github/github-mcp-server`). Defaults to `https://api.mcp.github.com`.

```bash
export MCP_REGISTRY_URL=https://mcp.internal.example.com
```

Scope is process-level: it applies to any shell that exports it and to child processes APM spawns. There is no per-project override yet. When the variable is set, `apm mcp search/list/show` print a one-line `Registry: <url>` diagnostic so you always know which endpoint was queried.

## Next Steps

- [Dependencies & Lockfile](../dependencies/#mcp-dependency-formats) -- the full `apm.yml` MCP grammar (overlays, `${input:...}`, package selection).
- [CLI Reference](../../reference/cli-commands/) -- every `apm install` flag in one place.
- [IDE & Tool Integration](../../integrations/ide-tool-integration/#mcp-model-context-protocol-integration) -- where each client reads MCP config from on disk.
- [Plugins](../plugins/#mcp-server-definitions) -- ship MCP servers as part of a plugin package.
- [Security Model](../../enterprise/security/) -- trust boundary, transitive-server policy, and how `--trust-transitive-mcp` fits in.
