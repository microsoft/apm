# Plugins

APM supports plugins through the `plugin.json` format. Plugins are automatically detected and integrated into your project as standard APM dependencies.

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
   - `.github/plugin/plugin.json` (GitHub Copilot format)
   - `.claude-plugin/plugin.json` (Claude format)
   - `plugin.json` (root)
3. **Map Artifacts** - Plugin primitives from the repository root are mapped into `.apm/`:
   - `agents/` → `.apm/agents/`
   - `skills/` → `.apm/skills/`
   - `commands/` → `.apm/prompts/`
    - `*.md` command files are normalized to `*.prompt.md` for prompt/command integration
4. **Synthesize** - `apm.yml` is automatically generated from plugin metadata
5. **Integrate** - The plugin is now a standard dependency with:
   - Version pinning via `apm.lock`
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
│       └── plugin.json   # GitHub Copilot location (highest priority)
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
│   └── plugin.json       # Claude location (second priority)
├── agents/
│   └── agent-name.agent.md
├── skills/
│   └── skill-name/
│       └── SKILL.md
└── commands/
    └── command-1.md
    └── command-2.md
```

#### Legacy APM Format
```
plugin-repo/
├── plugins/
│   └── plugin.json       # Legacy APM location (third priority)
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
├── plugin.json           # Root location (lowest priority)
├── agents/
│   └── agent-name.agent.md
├── skills/
│   └── skill-name/
│       └── SKILL.md
└── commands/
    └── command-1.md
    └── command-2.md
```

**Priority Order**: APM searches for `plugin.json`:
1. `plugin.json` (root) - checked first
2. Then recursively in subdirectories (e.g., `.github/plugin/`, `.claude-plugin/`)

**Note**: Primitives (agents, skills, commands, instructions) are always located at the repository root, regardless of where `plugin.json` is located.

### plugin.json Manifest

Required fields:

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

Run `apm install` to download and lock versions in `apm.lock`.

## Supported Hosts

- **GitHub** - `owner/repo` or `owner/repo/plugin-path`
- **GitHub** - GitHub URLs or SSH references
- **Azure DevOps** - `dev.azure.com/org/project/repo`

## Lock File Integration

Plugin versions are automatically tracked in `apm.lock`:

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

## Finding Plugins

Plugins can be found through:
- GitHub repositories (search for repos with `plugin.json`)
- Organization-specific plugin repositories
- Community plugin collections

Once found, install them using the standard `apm install owner/repo/plugin-name` command.

## Troubleshooting

### Plugin Not Detected

If APM doesn't recognize your plugin:

1. Check `plugin.json` exists at the repository root or in a subdirectory:
   - `plugin.json` (root )
   - `.github/plugin/plugin.json` (GitHub Copilot format)
   - `.claude-plugin/plugin.json` (Claude format)
2. Verify JSON is valid: `cat plugin.json | jq .`
3. Ensure required fields are present: `name`, `version`, `description`
4. Verify primitives are at the repository root (`agents/`, `skills/`, `commands/`)

### Version Resolution Issues

See the [concepts.md](./concepts.md) guide on dependency resolution.

### Custom Hosts / Private Repositories

See [integration-testing.md](./integration-testing.md) for enterprise setup.
