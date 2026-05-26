---
title: "Install LSP servers"
description: "Declare LSP servers in apm.yml and let apm install wire them into Claude Code."
---

`apm install` handles three dependency kinds: APM packages
(see [Install Packages](../install-packages/)), MCP servers
(see [Install MCP servers](../install-mcp-servers/)), and LSP servers.
This page covers LSP servers: how you declare them, what gets written,
and how the install pipeline manages their lifecycle.

LSP integration currently targets **Claude Code only**. The dependency
model is runtime-agnostic, so support for additional runtimes can be
added as they adopt LSP plugin configuration. For Claude Code's LSP
specification, see the
[Plugins reference](https://code.claude.com/docs/en/plugins-reference).

## One-line answer

Declare an LSP server in `apm.yml` and run `apm install`:

```yaml
dependencies:
  lsp:
    - name: gopls
      command: gopls
      args: ["serve"]
      extensionToLanguage:
        ".go": go
```

```bash
apm install
```

APM writes a `.lsp.json` at the project root (or updates `lspServers`
in `~/.claude.json` when installed with `-g`). Claude Code reads
this file and starts the configured language servers automatically.

## The `lsp:` section in apm.yml

LSP servers live under `dependencies.lsp:` (or `devDependencies.lsp:`).
Two forms are valid:

```yaml
dependencies:
  lsp:
    # 1. String reference (server name only -- resolved from
    #    transitive packages or plugin .lsp.json)
    - gopls

    # 2. Full object (self-contained server definition)
    - name: pyright
      command: pyright-langserver
      args: ["--stdio"]
      extensionToLanguage:
        ".py": python
        ".pyi": python
      transport: stdio
      env:
        PYTHONPATH: "./src"
      startupTimeout: 10000
```

The full field reference is in the
[Manifest schema](../../reference/manifest-schema/#43-dependencieslsp----listlspdependency).

## What `apm install` writes to disk

| Scope | File | Format |
|---|---|---|
| Project (default) | `.lsp.json` at project root | JSON: server name as key, config as value |
| User (`-g`) | `~/.claude.json` | JSON: `lspServers` section |

**Project-scope `.lsp.json` example:**

```json
{
  "gopls": {
    "command": "gopls",
    "args": ["serve"],
    "extensionToLanguage": {
      ".go": "go"
    }
  }
}
```

**User-scope `~/.claude.json` excerpt:**

```json
{
  "lspServers": {
    "gopls": {
      "command": "gopls",
      "args": ["serve"],
      "extensionToLanguage": {
        ".go": "go"
      }
    }
  }
}
```

## Required and optional fields

Two fields are required for every LSP server definition (object form):

| Field | Type | Description |
|---|---|---|
| `command` | `string` | Binary to execute. Must be on `$PATH` or a relative path. |
| `extensionToLanguage` | `map<string, string>` | Maps file extensions to LSP language identifiers (e.g. `".go": "go"`). |

Optional fields give you finer control:

| Field | Type | Default | Description |
|---|---|---|---|
| `args` | `list<string>` | `[]` | Command-line arguments. |
| `transport` | `string` | `stdio` | `stdio` or `socket`. |
| `env` | `map<string, string>` | `{}` | Environment variables set when starting the server. |
| `initializationOptions` | `any` | -- | Options passed during LSP initialization. |
| `settings` | `any` | -- | Settings passed via `workspace/didChangeConfiguration`. |
| `workspaceFolder` | `string` | -- | Workspace folder path. |
| `startupTimeout` | `int` | -- | Max time (ms) to wait for server startup. |
| `shutdownTimeout` | `int` | -- | Max time (ms) for graceful shutdown. |
| `restartOnCrash` | `bool` | -- | Restart the server automatically on crash. |
| `maxRestarts` | `int` | -- | Maximum restart attempts before giving up. |

## Transitive LSP dependencies

When an APM package you depend on declares its own `dependencies.lsp`
entries, APM collects them transitively after installation. Direct
(root) dependencies take precedence: if the root manifest and a
transitive package both declare a server with the same name, the
root definition wins.

Unlike MCP, LSP has no registry vs self-defined distinction. All
LSP servers from installed packages are treated as trusted.

## Stale server cleanup

When a previously installed LSP server is no longer declared by
any dependency, APM removes it from `.lsp.json` (or `~/.claude.json`
at user scope). The lockfile tracks which servers APM manages, so
hand-added servers are never touched.

## Lockfile

`apm install` persists two fields in `apm.lock.yaml`:

- `lsp_servers` -- sorted list of APM-managed server names.
- `lsp_configs` -- server-name-to-config baseline for drift detection.

See the [Lockfile specification](../../reference/lockfile-spec/).

## Plugin extraction

When APM installs a Claude Code plugin that contains `lspServers` in
`plugin.json` or a `.lsp.json` file, the LSP servers are automatically
extracted and wired into the install pipeline. The `${CLAUDE_PLUGIN_ROOT}`
placeholder in server configs is replaced with the absolute plugin path.

## Runtime support

LSP integration currently writes configuration for Claude Code only.
The `LSPDependency` model and manifest format are runtime-agnostic --
as other runtimes adopt LSP plugin configuration, APM can add write
targets without changing the dependency schema.

| Runtime | LSP support |
|---|---|
| Claude Code | `.lsp.json` / `~/.claude.json` |
| Cursor | Uses LSP internally; no external config format yet |
| OpenCode | Has LSP integration; no APM adapter yet |
| Others | Not yet supported |

## Next

- Full field reference and validation rules --
  [Manifest schema](../../reference/manifest-schema/#43-dependencieslsp----listlspdependency).
- Lockfile fields --
  [Lockfile specification](../../reference/lockfile-spec/).
- Claude Code LSP plugin authoring --
  [Plugins reference](https://code.claude.com/docs/en/plugins-reference).
