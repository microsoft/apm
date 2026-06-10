---
title: "Install LSP servers"
description: "Declare LSP servers in apm.yml and let apm install wire them into supported runtimes."
sidebar:
  order: 5
---

`apm install` handles three dependency kinds: APM packages
(see [Install Packages](../install-packages/)), MCP servers
(see [Install MCP servers](../install-mcp-servers/)), and LSP servers.
This page covers LSP servers: how you declare them, what gets written,
and how the install pipeline manages their lifecycle.

LSP integration targets supported agent runtimes. Today APM writes
configuration for Claude Code and GitHub Copilot CLI, while keeping the
manifest dependency model runtime-agnostic. See Claude Code's
[Plugins reference](https://code.claude.com/docs/en/plugins-reference)
and GitHub's
[Copilot CLI LSP servers documentation](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/lsp-servers)
for runtime-specific config details.

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

APM writes runtime-specific config for each detected target. Claude Code
uses `.lsp.json` or `~/.claude.json`; Copilot CLI uses `.github/lsp.json`
or `~/.copilot/lsp-config.json`. The runtime starts the configured
language servers automatically.

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

| Runtime | Project file | User file (`-g`) | Language map key |
|---|---|---|---|
| Claude Code | `.lsp.json` | `~/.claude.json` `lspServers` | `extensionToLanguage` |
| GitHub Copilot CLI | `.github/lsp.json` `lspServers` | `~/.copilot/lsp-config.json` `lspServers` | `fileExtensions` |

**Claude Code project-scope `.lsp.json` example:**

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

**Copilot CLI project-scope `.github/lsp.json` example:**

```json
{
  "lspServers": {
    "gopls": {
      "command": "gopls",
      "args": ["serve"],
      "fileExtensions": {
        ".go": "go"
      }
    }
  }
}
```

User-scope files keep the same runtime-specific server shape under their
`lspServers` section.

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
any dependency, APM removes it from the target runtime configs it manages.
The lockfile tracks which servers APM manages, so hand-added servers are
never touched.

## Lockfile

`apm install` persists two fields in `apm.lock.yaml`:

- `lsp_servers` -- sorted list of APM-managed server names.
- `lsp_configs` -- server-name-to-config baseline for drift detection.

See the [Lockfile specification](../../reference/lockfile-spec/).

## Plugin extraction

When APM installs a plugin that contains `lspServers` in `plugin.json`
or a `.lsp.json` file, the LSP servers are automatically extracted and
wired into the install pipeline. Plugin `.lsp.json` files may use either
a flat server map or a `{ "lspServers": { ... } }` envelope. The
`${CLAUDE_PLUGIN_ROOT}` placeholder in server configs is replaced with
the absolute plugin path for legacy Claude Code plugin compatibility.

## Runtime support

LSP integration writes configuration for supported runtimes and leaves
the manifest schema runtime-neutral. Target selection follows the same
runtime detection and `--target`/`targets:` mechanics as MCP installs.

| Runtime | LSP support |
|---|---|
| Claude Code | `.lsp.json` / `~/.claude.json` |
| GitHub Copilot CLI | `.github/lsp.json` / `~/.copilot/lsp-config.json` |
| Others | Not yet supported |

## Next

- Full field reference and validation rules --
  [Manifest schema](../../reference/manifest-schema/#43-dependencieslsp----listlspdependency).
- Lockfile fields --
  [Lockfile specification](../../reference/lockfile-spec/).
- Runtime-specific LSP config docs --
  [Claude Code Plugins reference](https://code.claude.com/docs/en/plugins-reference)
  and [Copilot CLI LSP servers](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/lsp-servers).
