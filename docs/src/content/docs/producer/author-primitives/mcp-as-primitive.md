---
title: "MCP servers as a primitive"
description: "Ship MCP server configuration with your APM package so consumers get it wired into every harness on apm install."
---

When a consumer runs `apm install` against your package, the `mcp:`
block in your `apm.yml` becomes their MCP server config. No README
copy-paste, no per-harness JSON. This page is the producer side of
[Install MCP servers](../../../consumer/install-mcp-servers/).

## One-line answer

Add a `dependencies.mcp:` section to your package's `apm.yml`:

```yaml
dependencies:
  mcp:
    - io.github.github/github-mcp-server
```

On the consumer's machine, `apm install <your-package>` writes this
into every detected harness's MCP config file.

## What "MCP as a primitive" means here

[Primitives and targets](../../../concepts/primitives-and-targets/)
lists MCP servers as a primitive APM routes per target. Unlike
`.apm/skills/` or `.apm/prompts/`, MCP servers do not live as files in
your package -- they live as **declarations** in `apm.yml`. APM
materialises them at install time into the harness-specific config
file (see the per-harness map in
[Install MCP servers](../../../consumer/install-mcp-servers/#what-apm-install-writes-to-disk)).

You declare once. APM writes `.vscode/mcp.json`, `.cursor/mcp.json`,
`~/.claude.json`, `~/.codex/config.toml`, and the rest -- whichever
harnesses the consumer has.

## The `mcp:` schema

Each entry under `dependencies.mcp:` (or `devDependencies.mcp:`) is
either a bare string or a mapping. Fields, from
`src/apm_cli/models/dependency/mcp.py`:

| Field | Required when | Notes |
|---|---|---|
| `name` | always (mapping form) | Matches `^[a-zA-Z0-9@_][a-zA-Z0-9._@/:=-]{0,127}$`. |
| `transport` | self-defined | One of `stdio`, `http`, `sse`, `streamable-http`. |
| `command` | self-defined `stdio` | Single binary path; no whitespace (use `args`). |
| `args` | optional | List for self-defined; dict for registry overlays. |
| `url` | self-defined `http`/`sse`/`streamable-http` | `http://` or `https://` only. |
| `env` | optional, stdio | Map of env vars passed to the child process. |
| `headers` | optional, remote | Map of HTTP headers. CR/LF rejected. |
| `tools` | optional | Allowlist of tool names. Default `["*"]`. |
| `version` | optional | Pin a registry server version. |
| `registry` | optional | `false` = self-defined; URL = custom registry. |
| `package` | optional | `npm`, `pypi`, or `oci` for registry-resolved servers. |

Three forms cover every case:

```yaml
dependencies:
  mcp:
    # 1. Registry reference -- resolved from api.mcp.github.com
    - io.github.github/github-mcp-server

    # 2. Self-defined stdio
    - name: filesystem
      registry: false
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]

    # 3. Self-defined remote
    - name: linear
      registry: false
      transport: http
      url: https://mcp.linear.app/sse
      headers:
        Authorization: "Bearer ${LINEAR_TOKEN}"
```

## What the consumer sees on install

For each detected harness, APM writes the relevant MCP config file.
The full mapping (file path, scope, JSON/TOML schema) is in
[Install MCP servers](../../../consumer/install-mcp-servers/#what-apm-install-writes-to-disk).
You do not need to know it to author -- APM handles the per-target
translation.

The consumer can run `apm mcp list` to confirm the server landed in
each runtime they care about.

## Secrets: never commit, always indirect

Treat `apm.yml` like `package.json`: it is committed, reviewed, and
shipped. Do not embed tokens. Two patterns work:

```yaml
# Env-var indirection -- the harness expands at runtime
- name: linear
  registry: false
  transport: http
  url: https://mcp.linear.app/sse
  headers:
    Authorization: "Bearer ${LINEAR_TOKEN}"

# Stdio env -- value passed verbatim; use ${VAR} for indirection
- name: my-internal
  registry: false
  transport: stdio
  command: my-server
  env:
    API_TOKEN: "${MY_API_TOKEN}"
```

Headers and env values are not shell-expanded by APM -- the harness
does that when it spawns the server or makes the request. Keep the
real secret in the consumer's environment (or their secret manager).

The `github-mcp-server` is a special case: APM injects an
`Authorization: Bearer <token>` header automatically when it writes
the Copilot CLI config. See
[Token injection](../../../consumer/install-mcp-servers/#token-injection-github-mcp-server).

## Direct vs transitive: the trust boundary

A self-defined MCP server (`registry: false`) declared by your package
is trusted only when your package is a **direct** dependency of the
consumer. If your package is pulled in transitively, APM warns and
**skips** the MCP entry unless the consumer passes
`--trust-transitive-mcp`. Source:
`src/apm_cli/integration/mcp_integrator_install.py` and
`src/apm_cli/integration/mcp_integrator.py`.

Implications for producers:

- Registry-resolved servers (form 1 above) flow through transitively
  without a trust prompt -- they are vetted by the registry.
- Self-defined `stdio` and remote servers should be reserved for
  things the consumer would expect from a direct dependency.
- Document any self-defined MCP server in your README so a transitive
  consumer knows what they would be trusting.

For the full trust model, see
[Lifecycle](../../../concepts/lifecycle/) and
[Security](../../../enterprise/security/).

## Pitfalls

- **Whitespace in `command`**: APM does not split on spaces. Put the
  binary in `command` and arguments in `args`. Validation rejects
  `command: "npx -y server"` with a fix-it message.
- **Hard-coded paths**: `command: /Users/me/bin/server` works on your
  laptop and nowhere else. Prefer a binary on `PATH` (`npx`, `uvx`)
  or a runtime-installed package.
- **`url:` with non-`http(s)` scheme**: rejected at parse time.
  WebSocket and `file://` are not supported transports.
- **Embedded tokens in `headers` or `env` literals**: a reviewer will
  see them. Use `${VAR}` indirection.
- **Forgetting `devDependencies.mcp:`**: an MCP server you only need
  for development (a local mock, a debug bridge) belongs in
  `devDependencies.mcp:`. `apm pack` excludes it; consumers do not
  get it. See
  [dev-only primitives](../../../guides/dev-only-primitives/).

## Next

- Bundle other primitives alongside MCP servers --
  [skills](../skills/), [prompts](../prompts/),
  [hooks and commands](../hooks-and-commands/).
- Pack and ship -- [Pack a bundle](../../pack-a-bundle/).
