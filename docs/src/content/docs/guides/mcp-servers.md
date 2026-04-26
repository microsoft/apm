---
title: "MCP Servers"
description: "Add MCP servers to your project with apm install --mcp. Supports stdio, registry, and remote HTTP servers across Copilot, Claude, Cursor, Codex, OpenCode, and Gemini."
sidebar:
  order: 6
---

APM manages your agent configuration in `apm.yml` -- think `package.json` for AI. MCP servers are dependencies in that manifest.

`apm install --mcp` adds a server to `apm.yml` and wires it into every detected client (Copilot, Claude, Cursor, Codex, OpenCode, Gemini) in one step.

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

`NAME` is the entry that lands under `dependencies.mcp` in `apm.yml`. It must match `^[a-zA-Z0-9@_][a-zA-Z0-9._@/:=-]{0,127}$`.

| Flag | Purpose |
|------|---------|
| `--mcp NAME` | Add `NAME` to `dependencies.mcp` and install it. Required to enter this code path. |
| `--transport stdio\|http\|sse\|streamable-http` | Override transport. Inferred from `--url` (remote) or post-`--` argv (stdio) when omitted. |
| `--url URL` | Endpoint for `http` / `sse` transports. Scheme must be `http` or `https`. |
| `--env KEY=VALUE` | Environment variable for stdio servers. Repeatable. |
| `--header KEY=VALUE` | HTTP header for remote servers. Repeatable. Requires `--url`. |
| `--mcp-version VER` | Pin the registry entry to a specific version. |
| `--registry URL` | Custom MCP registry URL (`http://` or `https://`) for resolving the registry-form `NAME`. Overrides the `MCP_REGISTRY_URL` env var. Captured in `apm.yml` on the entry's `registry:` field for auditability. Not valid with `--url` or a stdio command (self-defined entries). |
| `--dev` | Add to `devDependencies.mcp` instead of `dependencies.mcp`. |
| `--force` | Replace an existing entry with the same `NAME` without prompting (CI). |
| `--dry-run` | Print what would be added; do not write `apm.yml` or touch client configs. |
| `-- COMMAND ARGV...` | Everything after `--` is the stdio command for the server. Implies `--transport stdio`. |

`apm mcp install NAME ...` is an alias that forwards to `apm install --mcp NAME ...`.

Inherited flags that still apply: `--runtime`, `--exclude`, `--verbose`. Flags that do **not** apply with `--mcp`: `--global` (MCP entries are project-scoped), `--only apm`, `--update`, `--ssh` / `--https` / `--allow-protocol-fallback` -- see [Errors and Conflicts](#errors-and-conflicts).

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
| `NAME` shape | `^[a-zA-Z0-9@_][a-zA-Z0-9._@/:=-]{0,127}$` | Keeps names round-trippable as YAML keys, file paths, and registry identifiers. Leading `-` is rejected (argv flag confusion) and leading `.` is rejected (dotfile / relative-path confusion). Leading `_` is allowed for private/internal naming conventions. |
| `--url` scheme | `http` or `https` only | Blocks `file://`, `gopher://`, and similar exfil vectors. |
| `--registry` scheme | `http` or `https` only; `ws://`, `wss://`, `file://`, `javascript:` rejected | Same allowlist as `--url`. Length capped at 2048 chars. Empty / schemeless values fail with a usage error. |
| `--header` content | No CR or LF in keys or values | Prevents header injection / response splitting. |
| `command` (stdio) | No path-traversal segments (`..`, absolute escapes) | Blocks an entry from pointing the client at a binary outside the project. |
| Internal / metadata `--url` | Warning, not blocked | Catches accidental cloud-metadata-IP URLs without breaking valid intranet servers. |
| `--env` shell metacharacters | Warning, not blocked | Reminds you that stdio servers do not go through a shell, so `$VAR` and backticks are passed literally. |

Self-defined servers (everything except the bare-string registry form) additionally require:

- `transport` -- one of `stdio`, `http`, `sse`, `streamable-http`. These are MCP transport names, not URL schemes: remote variants connect over HTTPS.
- `url` -- when `transport` is `http`, `sse`, or `streamable-http`.
- `command` -- when `transport` is `stdio`.

For the trust boundary on transitive MCP servers (`--trust-transitive-mcp`), see [Dependencies: Trust Model](../dependencies/#mcp-dependency-formats) and [Security Model](../../enterprise/security/).

## Errors and Conflicts

`apm install --mcp` rejects flag combinations that would silently do the wrong thing. All conflicts exit with code 2.

| Error | Trigger | Fix |
|-------|---------|-----|
| `cannot mix --mcp with positional packages` | `apm install owner/repo --mcp foo` | Run `--mcp` and APM-package installs as separate commands. |
| `MCP servers are project-scoped; --global is not supported for MCP entries` | `apm install -g --mcp foo` | MCP servers always land in the project `apm.yml`. Drop `-g`. |
| `cannot use --only apm with --mcp` | Filtering by APM-only while adding an MCP entry. | Drop `--only apm`. |
| `--header requires --url` | `--header` without an HTTP/SSE endpoint. | Add `--url`, or use `--env` for stdio servers. |
| `cannot specify both --url and a stdio command` | Mixed remote + post-`--` argv. | Pick one shape. |
| `stdio transport doesn't accept --url` | `--transport stdio --url ...` | Use post-`--` argv for stdio. |
| `remote transports don't accept stdio command` | `--transport http -- npx ...` | Drop `--transport http` (or drop the post-`--` argv). |
| `--env applies to stdio MCPs; use --header for remote` | `--env` on a remote server. | Use `--header` for HTTP/SSE auth. |
| `--registry only applies to registry-resolved MCP servers; remove --url or the post-`--` stdio command, or drop --registry` | `--registry` combined with `--url` or a stdio command. | `--registry` only steers the registry resolver; self-defined entries do not consult a registry. |

Existing-entry conflicts (`already exists in apm.yml`) are covered in [Updating and Replacing Servers](#updating-and-replacing-servers).

## Custom registry (enterprise)

APM resolves the MCP registry endpoint with the following precedence (highest first):

1. **`apm install --mcp NAME --registry URL`** -- per-install CLI flag. Captured in `apm.yml` on the entry's `registry:` field for auditability so reviewers can see which registry each MCP server was resolved against.
2. **`MCP_REGISTRY_URL` env var** -- process-level override for `apm mcp` discovery commands and `apm install --mcp` when the flag is not given. Not written to `apm.yml`.
3. **Default**: `https://api.mcp.github.com`.

Enterprises with **both** a public and a private registry use `--registry` to pick the private one explicitly per server, leaving `MCP_REGISTRY_URL` unset (or pointed at whichever registry should be the default for `apm mcp search/list/show`). The CLI flag wins:

```bash
# Server resolved against an internal registry, persisted to apm.yml
apm install --mcp acme/internal-server --registry https://mcp.internal.example.com

# Server resolved against the public default
apm install --mcp io.github.github/github-mcp-server
```

In `apm.yml`, the per-server `registry:` URL is captured so reviewers can audit what each MCP server resolves against:

```yaml title="apm.yml"
dependencies:
  mcp:
    - name: acme/internal-server
      registry: https://mcp.internal.example.com
    - io.github.github/github-mcp-server
```

A future [`apm config set mcp-registry-url`](https://github.com/microsoft/apm/issues/818) command will let you set a per-project default registry without exporting an env var. Until then, use the CLI flag for per-server overrides and the env var for shell-scoped defaults.

`MCP_REGISTRY_URL` overrides the MCP registry endpoint that APM queries when no `--registry` flag is present. It applies to all `apm mcp` discovery commands (`search`, `list`, `show`) and to `apm install --mcp` when resolving registry-form servers (e.g. `apm install --mcp io.github.github/github-mcp-server`). Defaults to `https://api.mcp.github.com`.

```bash
export MCP_REGISTRY_URL=https://mcp.internal.example.com
```

Scope is process-level: it applies to any shell that exports it and to child processes APM spawns. There is no per-project override yet. When the variable is set, `apm mcp search/list/show` print a one-line `Registry: <url>` diagnostic so you always know which endpoint was queried.

### URL validation and security

APM validates `MCP_REGISTRY_URL` and `--registry` at startup. The URL must include a scheme and host (e.g. `https://mcp.internal.example.com`); schemeless values, empty strings, and unsupported schemes (anything other than `http`/`https`) are rejected with an actionable error. URLs longer than 2048 characters are rejected.

For the **env var** path, plaintext `http://` is **rejected by default** to prevent token leakage and tampering. For development or air-gapped intranets where TLS is genuinely impractical, opt in explicitly:

```bash
export MCP_REGISTRY_ALLOW_HTTP=1
export MCP_REGISTRY_URL=http://mcp.internal.example.com
```

For the **`--registry` CLI flag**, both `http://` and `https://` are accepted without an opt-in: the explicit, per-invocation user intent is treated as a strong signal, matching how npm-style tools handle private/local registries on intranets. Production deployments should still prefer `https://`.

When a custom registry is set and unreachable during install pre-flight, APM treats the network error as **fatal** instead of silently assuming servers exist. This prevents a misconfigured or down enterprise registry from quietly approving every MCP dependency. The default registry (`https://api.mcp.github.com`) keeps the existing assume-valid behaviour for transient errors so unrelated network blips do not block installs.

## Next Steps

- [Dependencies & Lockfile](../dependencies/#mcp-dependency-formats) -- the full `apm.yml` MCP grammar (overlays, `${input:...}`, package selection).
- [CLI Reference](../../reference/cli-commands/) -- every `apm install` flag in one place.
- [IDE & Tool Integration](../../integrations/ide-tool-integration/#mcp-model-context-protocol-integration) -- where each client reads MCP config from on disk.
- [Plugins](../plugins/#mcp-server-definitions) -- ship MCP servers as part of a plugin package.
- [Security Model](../../enterprise/security/) -- trust boundary, transitive-server policy, and how `--trust-transitive-mcp` fits in.
