---
title: apm mcp
description: Discover and inspect MCP servers in the registry
sidebar:
  order: 22
---

Discover, inspect, and install MCP servers from the public MCP registry
(or an enterprise mirror).

## Synopsis

```bash
apm mcp list [--limit N] [--verbose]
apm mcp search QUERY [--limit N] [--verbose]
apm mcp show SERVER_NAME [--verbose]
apm mcp install NAME [-- runtime args...]
```

## Description

`apm mcp` groups read-only registry queries (`list`, `search`, `show`)
plus a thin install alias.

The canonical install path for MCP servers is
[`apm install --mcp NAME`](../install/#mcp-server-entry-use-only-with---mcp). It
edits `apm.yml`, resolves the registry entry, and writes the resulting
`mcpServers` block to your project. `apm mcp install` is a forwarder
that calls the same code path -- use whichever spelling you prefer.

For an end-to-end consumer walkthrough (declaring an MCP server in
`apm.yml`, configuring transport and credentials, deploying to a
runtime), see
[Install MCP servers](../../../consumer/install-mcp-servers/).

## Subcommands

### `apm mcp list`

List servers published to the registry.

```bash
apm mcp list                 # first 20 entries
apm mcp list --limit 50
```

| Flag | Description |
|---|---|
| `--limit N` | Number of results to show. Default: `20`. |
| `--verbose`, `-v` | Show detailed diagnostic output. |

### `apm mcp search`

Substring search across registry entries.

```bash
apm mcp search github
apm mcp search fetch --limit 5
```

| Flag | Description |
|---|---|
| `--limit N` | Number of results to show. Default: `10`. |
| `--verbose`, `-v` | Show detailed diagnostic output. |

### `apm mcp show`

Print full metadata for a single server: version, repository,
deployment type (remote endpoint and/or local package), and the steps
to add it to a project.

```bash
apm mcp show io.github.modelcontextprotocol/server-fetch
```

| Flag | Description |
|---|---|
| `--verbose`, `-v` | Show detailed diagnostic output. |

Exit code `1` if the server name is not present in the registry.

### `apm mcp install`

Alias for [`apm install --mcp NAME`](../install/#mcp-server-entry-use-only-with---mcp).
All flags and the post-`--` runtime command are forwarded verbatim.

```bash
apm mcp install fetch -- npx -y @modelcontextprotocol/server-fetch
apm mcp install api --transport http --url https://example.com/mcp
```

Common forwarded options (see `apm install --mcp --help` for the full
list):

| Flag | Description |
|---|---|
| `--transport [stdio\|http\|sse\|streamable-http]` | Transport type. |
| `--url URL` | Server URL for remote transports. |
| `--env KEY=VALUE` | Environment variable. Repeatable. |
| `--header KEY=VALUE` | HTTP header. Repeatable. |
| `--registry URL` | Custom registry URL for this invocation. |
| `--mcp-version VER` | Pin the registry entry to a specific version. |
| `--dev` | Add to `devDependencies`. |
| `--dry-run` | Resolve and print without writing `apm.yml`. |
| `--force` | Overwrite an existing entry. |
| `--no-policy` | Skip policy checks. |
| `--verbose`, `-v` | Verbose output. |

## Environment variables

| Variable | Effect |
|---|---|
| `MCP_REGISTRY_URL` | Override the registry endpoint used by `list`, `search`, `show`, and `install`. When set, every command prints a one-line `Registry: <url>` diagnostic so the override is visible. Unset: the public default registry is used silently. |

Network failures against an overridden registry surface an explicit
hint pointing at `MCP_REGISTRY_URL` so misconfigurations are easy to
spot in CI logs.

## Examples

Discover and inspect:

```bash
apm mcp search github
apm mcp show io.github.github/github-mcp-server
```

Install a stdio server with a runtime command:

```bash
apm mcp install fetch -- npx -y @modelcontextprotocol/server-fetch
```

Install a remote HTTP server:

```bash
apm mcp install api --transport http --url https://example.com/mcp \
  --header "Authorization=Bearer $TOKEN"
```

Point at an enterprise registry mirror:

```bash
export MCP_REGISTRY_URL=https://mcp.internal.example.com
apm mcp list
```

## Related

- [`apm install`](../install/) -- canonical MCP install path.
- [Install MCP servers](../../../consumer/install-mcp-servers/) -- consumer guide covering `apm.yml`, transports, and runtime deployment.
