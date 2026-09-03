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

For this root-manifest example, install `gopls` separately and make sure it is
available on `PATH`; APM configures the runtime but does not install external
language-server executables.

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
apm install --target claude
```

APM writes runtime-specific config for each detected target. At project scope,
Claude Code discovers LSP servers from the APM-managed plugin manifest at
`.claude/skills/apm-lsp/.claude-plugin/plugin.json`; user-scope installs use
`~/.claude/skills/apm-lsp/.claude-plugin/plugin.json`. Copilot CLI uses
`.github/lsp.json` or `~/.copilot/lsp-config.json`. This generated shape
matches each runtime's documented discovery contract.

Claude skills-directory plugin discovery requires Claude Code v2.1.157 or
newer. For project installs, accept Claude Code's workspace-trust prompt; LSP
servers start only after you trust the workspace. Start Claude from the
repository root so its primary working directory contains `.claude/skills/`:
project-scope skills-directory plugins do not walk up from a subdirectory to
the repo root. Personal-scope plugins under your home directory have no
workspace-trust gate. After APM reports that it configured or removed Claude
LSP servers, restart Claude Code or run `/reload-plugins` (use
`/reload-plugins --force` when Claude requests it). Open a file matching a
configured extension and confirm its LSP-backed diagnostics or navigation work
before relying on the integration. If another enabled Claude LSP server already
claims the same file extension, Claude uses the first registered server for
that extension and the others never start; for example, an APM-declared `.py`
server can lose to an installed `pyright-lsp`.

If an earlier APM version created a project-root `.lsp.json`, APM leaves it
unchanged because it may contain user-owned entries. Claude Code does not use
that file for project plugin discovery. Review it, migrate any entries you
still need, then remove it.

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
| Claude Code | `.claude/skills/apm-lsp/.claude-plugin/plugin.json` `lspServers` | `~/.claude/skills/apm-lsp/.claude-plugin/plugin.json` `lspServers` | `extensionToLanguage` |
| GitHub Copilot CLI | `.github/lsp.json` `lspServers` | `~/.copilot/lsp-config.json` `lspServers` | `fileExtensions` |

**Claude Code project-scope plugin manifest example:**

```json
{
  "name": "apm-lsp",
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
`lspServers` section. Claude skills-directory plugins are auto-discovered, so
APM does not write an `enabledPlugins` entry.

## Required and optional fields

Two fields are required for every LSP server definition (object form):

| Field | Type | Description |
|---|---|---|
| `command` | `string` | Binary to execute. Must resolve from `$PATH` or use an absolute or relative path. |
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

Unlike MCP, LSP has no registry vs self-defined distinction. LSP commands from
dependency packages pass the executable trust gate for their declaring package
when the gate is enabled. Approve the package with `apm approve <package>`
before APM exposes its server to a supported runtime. Without a project or org
`executables` opt-in, the compatibility default permits dependency
executables. Root-project LSP declarations are trusted as local project
content.

## Stale server cleanup

When a previously installed LSP server is no longer declared by
any dependency, APM removes it from the target runtime configs it manages.
The lockfile tracks which servers APM manages, so hand-added servers are
never touched. When cleanup removes the last managed server from an otherwise
empty APM-owned Claude project plugin, APM deletes the plugin and its empty
`apm-lsp` directory.

## Lockfile

`apm install` records resolved LSP configuration, declaration ownership, and
target ownership in `apm.lock.yaml`. These fields let lifecycle commands
reconcile only APM-owned entries. See the
[Lockfile specification](../../reference/lockfile-spec/) for the canonical
field definitions.

## Plugin extraction

When APM installs a plugin, it extracts LSP servers from an inline or
file-valued `lspServers` entry in `plugin.json`, or auto-discovers
`com.microsoft.apm/lsp.json`, `lsp.json`, or `.lsp.json`. The servers are
then wired into the install pipeline. Plugin LSP files may use either a flat
server map or a `{ "lspServers": { ... } }` envelope. The
`${CLAUDE_PLUGIN_ROOT}` placeholder in server configs is replaced with
the absolute plugin path for legacy Claude Code plugin compatibility.
These are source files shipped by a dependency package, distinct from the
`.claude-plugin/plugin.json` that APM generates for Claude discovery.
Plugins authored for Copilot CLI may use `fileExtensions` instead of
`extensionToLanguage` and `warmupTimeoutMs` instead of `startupTimeout`;
APM normalizes those aliases before validation. A non-null canonical value
wins when both are present; a null canonical value falls back to its alias.
APM ignores the unsupported Copilot `cwd` field and warns that the consumer
runtime chooses the working directory. Copilot output uses `fileExtensions`
and `warmupTimeoutMs`; canonical manifests and lockfiles retain
`extensionToLanguage` and `startupTimeout`.

## Runtime support

LSP integration writes configuration for supported runtimes and leaves
the manifest schema runtime-neutral. Target selection follows the same
effective decision as package and MCP installation: `--target` >
`apm.yml targets:` > `apm config set target ...` > auto-detect. If LSP
work is declared but no effective target supports LSP, or a native config
write fails, install exits non-zero with a next step instead of reporting
success.

| Runtime | LSP support |
|---|---|
| Claude Code | `.claude/skills/apm-lsp/.claude-plugin/plugin.json` / `~/.claude/skills/apm-lsp/.claude-plugin/plugin.json` |
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
