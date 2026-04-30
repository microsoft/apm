---
title: "Plugins"
sidebar:
  order: 4
---

APM supports plugins through the `plugin.json` format. Plugins are automatically detected and integrated into your project as standard APM dependencies.

## Plugin authoring

For where plugins fit relative to APM's source layout, see [Anatomy -- Why not just ship a `plugin.json`?](../../introduction/anatomy-of-an-apm-package/#why-not-just-ship-a-pluginjson).

Plugin ecosystems handle distribution but lack dependency management, security scanning, version locking, and dev/prod separation. As plugins depend on shared primitives, these gaps compound.

APM is the supply-chain layer. Author packages with full tooling — transitive dependencies, lockfile pinning, [security scanning](../../enterprise/security/), [`devDependencies`](../../reference/manifest-schema/#5-devdependencies) — then export as standard plugins. Consumers never need APM installed.

### Three modes

| Mode | Manifests | Use when |
|------|-----------|----------|
| **APM-only** | `apm.yml` | Full APM workflow — deploy to `.github/`, `.claude/`, `.cursor/`, `.opencode/` |
| **Plugin-only** | `plugin.json` | Standard plugin consumed by APM via `apm install` — metadata synthesized automatically |
| **Hybrid** | `apm.yml` + `plugin.json` | Author with dependency management + security, export as standalone plugins |

**APM-only** is the default for teams using APM end-to-end. **Plugin-only** requires no changes to existing plugins — APM consumes them as-is. **Hybrid** is for plugin authors who want APM's supply-chain features during development while distributing standard plugins.

### Hybrid authoring workflow

```bash
apm init my-plugin --plugin    # Creates both apm.yml and plugin.json
apm install --dev owner/helpers # Dev-only dependency (excluded from export)
apm install owner/core-rules   # Production dependency
apm pack                       # Export -- plugin format is the default; dev deps excluded, security scanned
```

The exported plugin directory contains no APM-specific files. See [Pack & Distribute -- Plugin format](../../guides/pack-distribute/#plugin-format-vs-apm-format) for the output mapping.

## Overview

Plugins are packages that contain:

- **Skills** - Reusable agent personas and expertise
- **Agents** - AI agent definitions
- **Commands** - Executable prompts and workflows  
- **Instructions** - Context and guidelines

APM automatically detects plugins with `plugin.json` manifests and synthesizes `apm.yml` from the metadata, treating them identically to other APM packages.

## Installation

Install plugins using the standard `apm install` command:

```bash
# Install a plugin from GitHub
apm install owner/repo/plugin-name

# Or add to apm.yml
dependencies:
  apm:
    - anthropics/claude-code-plugins/commit-commands#v1.2.0
```

## How APM Handles Plugins

When you run `apm install owner/repo/plugin-name`:

1. **Clone** - APM clones the repository to `apm_modules/`
2. **Detect** - It searches for `plugin.json` in priority order:
   1. `plugin.json` (root)
   2. `.github/plugin/plugin.json` (GitHub Copilot format)
   3. `.claude-plugin/plugin.json` (Claude format)
   4. `.cursor-plugin/plugin.json` (Cursor format)
3. **Map Artifacts** - Plugin primitives from the repository root are mapped into `.apm/`:
   - `agents/` → `.apm/agents/`
   - `skills/` → `.apm/skills/`
   - `commands/` → `.apm/prompts/`
    - `*.md` command files are normalized to `*.prompt.md` for prompt/command integration
4. **Synthesize** - `apm.yml` is automatically generated from plugin metadata
5. **Integrate** - The plugin is now a standard dependency with:
   - Version pinning via `apm.lock.yaml`
   - Transitive dependency resolution
   - Conflict detection
   - Everything else APM packages support

This unified approach means **no special commands needed** — plugins work exactly like any other APM package.

## Plugin Format

A plugin repository contains a `plugin.json` manifest and primitives at the repository root.

### Supported Plugin Structures

APM supports multiple plugin manifest locations to accommodate different platforms:

#### GitHub Copilot Format
```
plugin-repo/
├── .github/
│   └── plugin/
│       └── plugin.json   # GitHub Copilot location
├── agents/
│   └── agent-name.agent.md
├── skills/
│   └── skill-name/
│       └── SKILL.md
└── commands/
    └── command-1.md
    └── command-2.md
```

#### Claude Format
```
plugin-repo/
├── .claude-plugin/
│   └── plugin.json       # Claude location
├── agents/
│   └── agent-name.agent.md
├── skills/
│   └── skill-name/
│       └── SKILL.md
└── commands/
    └── command-1.md
    └── command-2.md
```

#### Root Format
```
plugin-repo/
├── plugin.json           # Root location (checked first)
├── agents/
│   └── agent-name.agent.md
├── skills/
│   └── skill-name/
│       └── SKILL.md
└── commands/
    └── command-1.md
    └── command-2.md
```

#### Cursor Format
```
plugin-repo/
├── .cursor-plugin/
│   └── plugin.json       # Cursor location
├── agents/
│   └── agent-name.md
├── skills/
│   └── skill-name/
│       └── SKILL.md
└── rules/
    └── my-rule.mdc
```

**Priority Order**: APM checks for `plugin.json` in these locations:
1. `plugin.json` (root)
2. `.github/plugin/plugin.json`
3. `.claude-plugin/plugin.json`
4. `.cursor-plugin/plugin.json`

**Note**: Primitives (agents, skills, commands, instructions) are always located at the repository root, regardless of where `plugin.json` is located.

### plugin.json Manifest

Only `name` is required. `version` and `description` are optional metadata:

```json
{
  "name": "Plugin Display Name",
  "version": "1.0.0",
  "description": "What this plugin does"
}
```

Optional fields:

```json
{
  "name": "My Plugin",
  "version": "1.0.0",
  "description": "A plugin for APM",
  "author": "Author Name",
  "license": "MIT",
  "repository": "owner/repo",
  "homepage": "https://example.com",
  "tags": ["ai", "coding"],
  "dependencies": [
    "another-plugin-id"
  ]
}
```

#### Custom component paths

By default APM looks for `agents/`, `skills/`, `commands/`, and `hooks/` directories at the plugin root. You can override these with custom paths using strings or arrays:

```json
{
  "name": "my-plugin",
  "agents": ["./agents/planner.md", "./agents/coder.md"],
  "skills": ["./skills/analysis", "./skills/review"],
  "commands": "my-commands/",
  "hooks": "hooks.json"
}
```

- **String** — single directory or file path
- **Array** — list of directories or individual files
- For **skills**, directories are preserved as named subdirectories (e.g., `./skills/analysis/` → `.apm/skills/analysis/SKILL.md`)
- For **agents**, directory contents are flattened into `.apm/agents/` (agents are flat files, not named directories)
- `hooks` also accepts an inline object: `"hooks": {"hooks": {"PreToolUse": [...]}}`

##### Target-specific hook files

When a package ships hooks for multiple tools, use target-specific filenames so
each tool receives only its own hooks:

| Filename pattern | Deployed to |
|---|---|
| `*-copilot-hooks.json` | GitHub Copilot only |
| `*-cursor-hooks.json` | Cursor only |
| `*-claude-hooks.json` | Claude Code only |
| `*-codex-hooks.json` | Codex CLI only |
| `*-gemini-hooks.json` | Gemini CLI only |
| Any other name (e.g. `hooks.json`, `telemetry-hooks.json`) | All targets |

Example directory tree for a multi-target hook package:

```
my-hooks-pkg/
  hooks/
    hooks.json              # deployed to all targets
    copilot-hooks.json      # Copilot only
    cursor-hooks.json       # Cursor only
    claude-hooks.json       # Claude Code only
```

#### MCP Server Definitions

Plugins can ship MCP servers that are automatically deployed through APM's MCP pipeline. Define servers using `mcpServers` in `plugin.json`:

```json
{
  "name": "my-plugin",
  "mcpServers": {
    "my-server": {
      "command": "npx",
      "args": ["-y", "my-mcp-server"]
    },
    "my-api": {
      "url": "https://api.example.com/mcp"
    }
  }
}
```

`mcpServers` supports three forms:
- **Object** — inline server definitions (as above)
- **String** — path to a JSON file containing `mcpServers`
- **Array** — list of JSON file paths (merged, last-wins on name conflicts)

When `mcpServers` is absent, APM auto-discovers `.mcp.json` at the plugin root (then `.github/.mcp.json` as fallback), matching Claude Code's auto-discovery behavior.

Servers with `command` are configured as `stdio` transport; servers with `url` use `http` (or the `type` field if it specifies `sse` or `streamable-http`). All plugin-defined MCP servers are treated as self-defined (`registry: false`).

**Trust model**: Self-defined MCP servers from direct dependencies (depth=1) are auto-trusted. Transitive dependencies require `--trust-transitive-mcp`. See [dependencies.md](../dependencies/#self-defined-servers) for details.

## Examples

### Installing Plugins from GitHub

```bash
# Install a specific plugin
apm install anthropics/claude-code-plugins/commit-commands

# With version
apm install anthropics/claude-code-plugins/commit-commands#v1.2.0
```

### Adding Multiple Plugins to apm.yml

```yaml
dependencies:
  apm:
    - anthropics/claude-code-plugins/commit-commands#v1.2.0
    - anthropics/claude-code-plugins/refactor-tools#v2.0
    - mycompany/internal-standards#main
```

Then sync and install:

```bash
apm install
```

### Version Management

Plugins support all standard APM versioning:

```yaml
dependencies:
  apm:
    # Latest version
    - owner/repo/plugin

    # Latest from branch
    - owner/repo/plugin#main

    # Specific tag
    - owner/repo/plugin#v1.2.0

    # Specific commit  
    - owner/repo/plugin#abc123
```

Run `apm install` to download and lock versions in `apm.lock.yaml`.

## Supported Hosts

- **GitHub** - `owner/repo` or `owner/repo/plugin-path`
- **GitHub** - GitHub URLs or SSH references
- **Azure DevOps** - `dev.azure.com/org/project/repo`

## Lock File Integration

Plugin versions are automatically tracked in `apm.lock.yaml`:

```yaml
apm_modules:
  anthropics/claude-code-plugins/commit-commands:
    resolved: https://github.com/anthropics/claude-code-plugins/commit-commands#v1.2.0
    commit: abc123def456789
```

This ensures reproducible installs across environments.

## Conflict Detection

APM automatically detects:

- Duplicate plugins from different sources
- Version conflicts between dependencies
- Missing transitive dependencies

Run with `--verbose` to see dependency resolution details:

```bash
apm install --verbose
```

## Compilation

Plugins are automatically compiled during `apm compile`:

```bash
apm compile
```

This:
- Generates `AGENTS.md` from plugin agents
- Integrates skills into the runtime
- Includes prompt primitives

## Exporting APM packages as plugins

Use the [hybrid authoring workflow](#hybrid-authoring-workflow) to develop plugins with APM's full tooling and export them as standalone plugin directories. See [Pack & Distribute -- Plugin format](../../guides/pack-distribute/#plugin-format-vs-apm-format) for the output mapping and structure.

## Finding Plugins

Plugins can be found through:
- **Marketplaces** -- curated `marketplace.json` indexes browsable with `apm marketplace browse` and searchable with `apm search QUERY@MARKETPLACE`. See the [Marketplaces guide](../marketplaces/) for setup.
- GitHub repositories (search for repos with `plugin.json`)
- Organization-specific plugin repositories

Install by name from a registered marketplace:

```bash
apm install code-review@acme-plugins
```

APM resolves marketplace entries to Git URLs, so marketplace-installed plugins get full version locking, security scanning, and governance. See [Marketplaces](../marketplaces/) for details.

For direct installs, use the standard `apm install owner/repo/plugin-name` command.

## Troubleshooting

### Plugin Not Detected

If APM doesn't recognize your plugin:

1. Check `plugin.json` exists in one of the checked locations:
   - `plugin.json` (root)
   - `.github/plugin/plugin.json` (GitHub Copilot format)
   - `.claude-plugin/plugin.json` (Claude format)
   - `.cursor-plugin/plugin.json` (Cursor format)
2. Verify JSON is valid: `cat plugin.json | jq .`
3. Ensure `name` field is present (only required field)
4. Verify primitives are at the repository root (`agents/`, `skills/`, `commands/`)

### Version Resolution Issues

See the [concepts.md](../../introduction/how-it-works/) guide on dependency resolution.

### Custom Hosts / Private Repositories

See [integration-testing.md](../../contributing/integration-testing/) for enterprise setup.
