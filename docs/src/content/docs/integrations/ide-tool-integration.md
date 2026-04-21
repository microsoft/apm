---
title: "IDE & Tool Integration"
sidebar:
  order: 3
---

APM is designed to work seamlessly with your existing development tools and workflows. This guide covers integration patterns, supported AI runtimes, and compatibility with popular development tools.

## APM + Spec-kit Integration

APM manages the **context foundation** and provides **advanced context management** for software projects. It works exceptionally well alongside [Spec-kit](https://github.com/github/spec-kit) for specification-driven development, as well as with other AI Native Development methodologies like vibe coding.

### APM: Context Foundation

APM provides the infrastructure layer for AI development:

- **Context Packaging**: Bundle project knowledge, standards, and patterns into reusable modules
- **Dynamic Loading**: Smart context composition based on file patterns and current tasks
- **Performance Optimization**: Optimized context delivery for large, complex projects
- **Memory Management**: Strategic LLM token usage across conversations

### Spec-kit: Specification Layer

When using Spec-kit for Specification-Driven Development (SDD), APM automatically integrates the Spec-kit constitution:

- **Constitution Injection**: When using `apm compile`, APM injects the Spec-kit `constitution.md` into the compiled instruction files (`AGENTS.md`)
- **Rule Enforcement**: All coding agents respect the non-negotiable rules governing your project
- **Contextual Augmentation**: Compiled output embeds your team's context modules after Spec-kit's constitution
- **SDD Enhancement**: Augments the Spec Driven Development process with additional context curated by your teams

### Integrated Workflow

```bash
# 1. Set up APM contextual foundation
apm init my-project && apm install

# 2. Optional: compile for Codex/OpenCode instructions, Gemini, etc.
# Spec-kit constitution is automatically included in compiled AGENTS.md
apm compile

# 3. AI workflows use both SDD rules and team context
```

**Key Benefits of Integration**:
- **Universal Context**: APM grounds any coding agent on context regardless of workflow
- **SDD Compatibility**: Perfect for specification-driven development approaches
- **Flexible Workflows**: Also works with traditional prompting and vibe coding
- **Team Knowledge**: Combines constitutional rules with team-specific context

## Running Agentic Workflows

For running agentic workflows locally, see the [Agent Workflows guide](../../guides/agent-workflows/).

> **User-scope deployment**: `apm install -g` deploys primitives to user-level directories (`~/.copilot/`, `~/.claude/`, etc.), making packages available across all projects. See [Global Installation](../../guides/dependencies/#global-user-scope-installation) for per-target coverage.

## VS Code Integration

APM works natively with VS Code's GitHub Copilot implementation.

> **Auto-Detection**: VS Code integration is automatically enabled when a `.github/` folder exists in your project. If neither `.github/` nor `.claude/` exists, `apm install` skips folder integration (packages are still installed to `apm_modules/`). To force integration regardless of folder presence, pass an explicit target (e.g. `apm install --target copilot`) or set `target:` in `apm.yml` -- the target's root folder will be created automatically.

### Native VS Code Primitives

VS Code implements core primitives for GitHub Copilot that APM integrates with:

- **Agents**: AI personas and workflows with `.agent.md` files in `.github/agents/` (legacy: `.chatmode.md` in `.github/chatmodes/`)
- **Instructions Files**: Modular instructions with `copilot-instructions.md` and `.instructions.md` files
- **Prompt Files**: Reusable task templates with `.prompt.md` files in `.github/prompts/`
- **Skills**: Structured capabilities with `SKILL.md` in `.github/skills/`

> **Note**: APM supports both the new `.agent.md` format and legacy `.chatmode.md` format. VS Code provides Quick Fix actions to migrate from `.chatmode.md` to `.agent.md`.

### Automatic Prompt and Agent Integration

APM automatically integrates prompts and agents from installed packages into VS Code's native structure:

```bash
# Install APM packages - integration happens automatically when .github/ exists
apm install microsoft/apm-sample-package

# Prompts are automatically integrated to:
# .github/prompts/*.prompt.md (verbatim copy, original filename preserved)

# Agents are automatically integrated to:
# .github/agents/*.agent.md (verbatim copy)

# Instructions are automatically integrated to:
# .github/instructions/*.instructions.md (verbatim copy, original filename)

# Hooks are automatically integrated to:
# .github/hooks/*.json (hook definitions with rewritten script paths)
```

**How Auto-Integration Works**:
- **Zero-Config**: Always enabled, works automatically with no configuration needed
- **Auto-Cleanup**: Removes integrated files when you uninstall or prune packages (tracked via `deployed_files` in `apm.lock.yaml`)
- **Collision Detection**: If a local file has the same name as a package file, APM skips it with a warning (use `--force` to overwrite)
- **Always Overwrite**: Package-owned files are always copied fresh -- no version comparison
- **Link Resolution**: Context links are resolved during integration

**Integration Flow**:
1. Run `apm install` to fetch APM packages
2. APM automatically creates `.github/prompts/`, `.github/agents/`, `.github/instructions/`, and `.github/hooks/` directories as needed
3. Discovers `.prompt.md`, `.agent.md`, `.instructions.md`, and hook `.json` files in each package
4. Copies prompts to `.github/prompts/` with their original filename (e.g., `accessibility-audit.prompt.md`)
5. Copies agents to `.github/agents/` with their original filename (e.g., `security.agent.md`)
6. Copies instructions to `.github/instructions/` with their original filename (e.g., `python.instructions.md`)
7. Copies hooks to `.github/hooks/` with their original filename and copies referenced scripts
8. If a local file already exists with the same name, skips with a warning (use `--force` to overwrite)
9. Records all deployed files in `apm.lock.yaml` under `deployed_files` per package
10. VS Code automatically loads all prompts, agents, instructions, and hooks for your coding agents
11. Run `apm uninstall` to automatically remove integrated primitives (using `deployed_files` manifest)

**Intent-First Discovery**:
Files keep their original names for natural autocomplete in VS Code:
- Type `/design` -- VS Code shows `design-review.prompt.md`
- Type `/accessibility` -- VS Code shows `accessibility-audit.prompt.md`
- Search by what you want to do, not where it comes from

**Example**: 
```bash
# Install package with auto-integration
apm install microsoft/apm-sample-package

# Result in VS Code:
# Prompts:
# .github/prompts/accessibility-audit.prompt.md  - Available in chat
# .github/prompts/design-review.prompt.md        - Available in chat
# .github/prompts/style-guide-check.prompt.md    - Available in chat

# Agents:
# .github/agents/design-reviewer.agent.md        - Available as chat mode
# .github/agents/accessibility-expert.agent.md   - Available as chat mode

# Instructions:
# .github/instructions/python.instructions.md    - Applied to matching files

# Use with natural autocomplete:
# Type: /design
# VS Code suggests: design-review.prompt.md
```

**VS Code Native Features**:
- All integrated prompts appear in VS Code's prompt picker
- All integrated agents appear in VS Code's chat mode selector
- Native chat integration with primitives
- Seamless `/prompt` command support
- File-pattern based instruction application
- Agent support for different personas and workflows

### Optional: Compiled Context with AGENTS.md

For tools that do not support granular primitive discovery (such as Gemini), `apm compile` produces an `AGENTS.md` file that merges instructions into a single document. This is not needed for GitHub Copilot, Claude, or Cursor, which read per-file instructions natively. OpenCode and Codex also read `AGENTS.md`, so run `apm compile` to deploy instructions there.

```bash
# Compile all local and dependency instructions into AGENTS.md
apm compile --target copilot

# Default distributed compilation creates focused AGENTS.md files per directory
# Use --single-agents for a single monolithic file (legacy mode)
apm compile --single-agents
```

AGENTS.md aggregates instructions, context, and optionally the Spec-kit constitution into a single document that GitHub Copilot reads as project-level guidance.

## Claude Integration

APM provides first-class support for Claude Code and Claude Desktop through native format generation.

> **Auto-Detection**: Claude integration is automatically enabled when a `.claude/` folder exists in your project. If neither `.github/` nor `.claude/` exists, `apm install` skips folder integration (packages are still installed to `apm_modules/`). To force integration regardless of folder presence, pass an explicit target (e.g. `apm install --target claude`) or set `target: claude` in `apm.yml` -- `.claude/` will be created automatically.

### Optional: Compiled Output for Claude

Running `apm compile` is optional for Claude Code, which reads deployed primitives natively via `apm install`. If you want a single `CLAUDE.md` instruction file (for example, for Claude Desktop), you can generate one:

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Merged project instructions for Claude (instructions only, using `@import` syntax) |

When you run `apm install`, APM integrates package primitives into Claude's native structure:

| Location | Purpose |
|----------|---------|
| `.claude/rules/*.md` | Instructions converted to Claude rules format (`applyTo:` mapped to `paths:` frontmatter) |
| `.claude/agents/*.md` | Sub-agents from installed packages (from `.agent.md` files) |
| `.claude/commands/*.md` | Slash commands from installed packages (from `.prompt.md` files) |
| `.claude/skills/{folder}/` | Skills from packages with `SKILL.md` or `.apm/` primitives |
| `.claude/settings.json` (hooks key) | Hooks from installed packages (merged into settings) |

### OpenCode (`.opencode/`)

APM natively integrates with OpenCode when a `.opencode/` directory exists in your project. Run `apm install` and APM automatically deploys primitives to OpenCode's native format:

| APM Primitive | OpenCode Destination | Format |
|---|---|---|
| Agents (`.agent.md`) | `.opencode/agents/*.md` | Markdown with YAML frontmatter |
| Prompts (`.prompt.md`) | `.opencode/commands/*.md` | Converted to command format |
| Skills (`SKILL.md`) | `.opencode/skills/{name}/SKILL.md` | Identical (agentskills.io standard) |
| MCP servers | `opencode.json` | `mcp` key with `command` array, `environment` |
| Instructions | Via `AGENTS.md` | Read natively by OpenCode |

**Setup**: Create a `.opencode/` directory in your project root, then run `apm install`. APM detects the directory and deploys automatically. OpenCode reads `AGENTS.md` natively for instructions.

> **Note**: OpenCode does not support hooks.

#### Cursor (`.cursor/`)

| Location | Purpose |
|----------|---------|
| `.cursor/rules/*.mdc` | Instructions converted to Cursor rules format |
| `.cursor/agents/*.md` | Sub-agents from installed packages |
| `.cursor/skills/{name}/SKILL.md` | Skills from installed packages |
| `.cursor/hooks.json` (hooks key) | Hooks from installed packages (merged into config) |
| `.cursor/hooks/{pkg}/` | Referenced hook scripts |
| `.cursor/mcp.json` | MCP server configurations |

#### Codex CLI (`.codex/`)

| APM Primitive | Codex Destination | Format |
|---|---|---|
| Skills (`SKILL.md`) | `.agents/skills/{name}/SKILL.md` | Identical (agentskills.io standard) |
| Agents (`.agent.md`) | `.codex/agents/*.toml` | Converted from Markdown to TOML |
| Hooks (`.json`) | `.codex/hooks.json` + `.codex/hooks/{pkg}/` | Merged JSON config with `_apm_source` markers |
| Instructions | Via `AGENTS.md` | Compile-only (`apm compile --target codex`) |

**Setup**: Create a `.codex/` directory in your project root, then run `apm install`. APM detects the directory and deploys automatically.

> **Note**: Skills deploy to `.agents/skills/` (the cross-tool agent skills standard directory), not `.codex/skills/`. Agents are transformed from `.agent.md` Markdown to `.toml` format.

### Automatic Agent Integration

APM automatically deploys agent files from installed packages into `.claude/agents/`:

```bash
# Install a package with agents
apm install danielmeppiel/design-guidelines

# Result:
# .claude/agents/security.md -- Sub-agent available for Claude Code
```

**How it works:**
1. `apm install` detects `.agent.md` and `.chatmode.md` files in the package
2. Copies each to `.claude/agents/` as `.md` files
3. `apm uninstall` automatically removes the package's agents

### Automatic Command Integration

APM automatically converts `.prompt.md` files from installed packages into Claude slash commands:

```bash
# Install a package with prompts
apm install microsoft/apm-sample-package

# Result:
# .claude/commands/accessibility-audit.md -- /accessibility-audit
# .claude/commands/design-review.md       -- /design-review
```

**How it works:**
1. `apm install` detects `.prompt.md` files in the package
2. Converts each to Claude command format in `.claude/commands/`
3. `apm uninstall` automatically removes the package's commands

### Automatic Skills Integration

APM automatically integrates skills from installed packages into `.github/skills/`:

```bash
# Install a package with skills
apm install ComposioHQ/awesome-claude-skills/mcp-builder

# Result:
# .github/skills/mcp-builder/SKILL.md -- Skill available for agents
# .github/skills/mcp-builder/...      -- Full skill folder copied
```

**Skill Folder Naming**: Uses the source folder name directly (e.g., `mcp-builder`, `design-guidelines`), not flattened paths.

**How skill integration works:**
1. `apm install` checks if the package contains a `SKILL.md` file
2. If `SKILL.md` exists: copies the entire skill folder to `.github/skills/{folder-name}/` (primary location)
3. If a `.claude/` directory exists: also copies to `.claude/skills/{folder-name}/` for Claude compatibility
4. Sub-skills inside `.apm/skills/` are promoted to top-level `.github/skills/` entries
5. `apm uninstall` removes the skill folder from both locations

### Automatic Hook Integration

APM automatically integrates hooks from installed packages. Hooks define lifecycle event handlers (e.g., `PreToolUse`, `PostToolUse`, `Stop`) supported by VS Code Copilot, Claude Code, and Cursor.

> **Note:** Hook packages must be authored in the target platform's native format. APM handles path rewriting and file placement but does not translate between hook schema formats (e.g., Claude's `command` key vs GitHub Copilot's `bash`/`powershell` keys, or event name casing differences).

```bash
# Install a package with hooks
apm install anthropics/claude-plugins-official/plugins/hookify

# VS Code result (.github/hooks/):
# .github/hooks/hookify-hooks.json            -- Hook definitions
# .github/hooks/scripts/hookify/hooks/*.py    -- Referenced scripts

# Claude result (.claude/settings.json):
# Hooks merged into .claude/settings.json hooks key
# Scripts copied to .claude/hooks/hookify/

# Cursor result (.cursor/hooks.json) — only when .cursor/ exists:
# Hooks merged into .cursor/hooks.json hooks key
# Scripts copied to .cursor/hooks/hookify/
```

**How hook integration works:**
1. `apm install` discovers hook JSON files in `.apm/hooks/` or `hooks/` directories
2. For VS Code: copies hook JSON to `.github/hooks/` and rewrites script paths
3. For Claude: merges hook definitions into `.claude/settings.json` under the `hooks` key
4. For Cursor: merges hook definitions into `.cursor/hooks.json` under the `hooks` key (only when `.cursor/` exists)
5. For Codex: merges hook definitions into `.codex/hooks.json` under the `hooks` key (only when `.codex/` exists)
6. Copies referenced scripts to the target location
7. Rewrites `${CLAUDE_PLUGIN_ROOT}` and relative script paths for the target platform
8. `apm uninstall` removes hook files and cleans up merged settings

### Optional: Target-Specific Compilation

Compilation is optional for Copilot, Claude, and Cursor, which read per-file instructions natively. For OpenCode and Codex, run `apm compile` to generate `AGENTS.md` for instructions. Also use it when targeting Gemini:

```bash
# Generate all formats (default)
apm compile

# Generate only Claude formats
apm compile --target claude
# Creates: CLAUDE.md (instructions only)

# Generate only VS Code/Copilot formats  
apm compile --target copilot
# Creates: AGENTS.md (instructions only)
```

> **Remember**: `apm compile` generates instruction files only. Use `apm install` to integrate prompts, agents, instructions, commands, and skills from packages.

### Claude Command Format

Generated commands follow Claude's native structure:

```markdown
<!-- APM Managed: microsoft/apm-sample-package@abc123 -->
# Design Review

Review the current design for accessibility and UI standards.

## Instructions
[Content from original .prompt.md]
```

### Example Workflow

```bash
# 1. Install packages (integrates agents, commands, and skills automatically)
apm install microsoft/apm-sample-package
apm install github/awesome-copilot/skills/review-and-refactor

# 2. Optional: compile instructions if not using Claude Code natively
# apm compile --target claude

# 3. In Claude Code, use:
#    /code-review     -- Runs the code review workflow
#    /gdpr-assessment -- Runs GDPR compliance check

# 4. CLAUDE.md provides project instructions automatically
# 5. Agents in .claude/agents/ are available as sub-agents
# 6. Skills in .claude/skills/ are available for agents to reference
```

### Claude Desktop Integration

Skills installed to `.github/skills/` are the primary location; when a `.claude/` directory exists, APM also copies skills to `.claude/skills/` for compatibility. Each skill folder contains a `SKILL.md` that defines the skill's capabilities and any supporting files.

Claude Desktop can use `CLAUDE.md` as its project instructions file. Optionally run `apm compile --target claude` to generate `CLAUDE.md` with `@import` syntax for organized instruction loading.

### Cleanup and Sync

APM maintains synchronization between packages and Claude primitives:

- **Install**: Adds rules, agents, commands, and skills for new packages, tracked via `deployed_files` in `apm.lock.yaml`
- **Uninstall**: Removes only that package's rules, agents, commands, and skill directories (as tracked in `apm.lock.yaml`). User-authored files are preserved.
- **Update**: Refreshes rules, agents, commands, and skills when package version changes
- **Virtual Packages**: Individual files and skills (e.g., `github/awesome-copilot/skills/review-and-refactor`) are tracked via `apm.lock.yaml` and removed correctly on uninstall

## Other IDE Support

### IDEs with GitHub Copilot

Any IDE with GitHub Copilot support works with APM's file-level integration. APM deploys primitives to `.github/`, which Copilot discovers automatically:

```bash
apm install microsoft/apm-sample-package

# GitHub Copilot picks up:
# .github/prompts/*.prompt.md
# .github/agents/*.agent.md
# .github/instructions/*.instructions.md
```

**Supported IDEs**: JetBrains (IntelliJ, PyCharm, WebStorm, etc.), Visual Studio, VS Code, and any IDE with GitHub Copilot integration.

### Cursor

APM natively integrates with Cursor when a `.cursor/` directory exists in your project. Run `apm install` and APM automatically deploys primitives to Cursor's native format:

| APM Primitive | Cursor Destination | Format |
|---|---|---|
| Instructions (`.instructions.md`) | `.cursor/rules/*.mdc` | Converted: `applyTo:` → `globs:` frontmatter |
| Agents (`.agent.md`) | `.cursor/agents/*.md` | Markdown with YAML frontmatter |
| Skills (`SKILL.md`) | `.cursor/skills/{name}/SKILL.md` | Identical (agentskills.io standard) |
| Hooks (`.json`) | `.cursor/hooks.json` + `.cursor/hooks/{pkg}/` | Merged JSON config |
| MCP servers | `.cursor/mcp.json` | Standard `mcpServers` JSON |

**Setup**: Create a `.cursor/` directory in your project root (or use Cursor's settings), then run `apm install`. APM detects the directory and deploys automatically.

**Fallback**: `apm compile` also generates `AGENTS.md` at the project root, which Cursor discovers as project-level context. This is useful for compiled/merged instruction output.

```bash
# Preview what will be compiled
apm compile --dry-run

# Compile with source attribution for traceability
apm compile --verbose

# Watch mode: auto-recompile when primitives change
apm compile --watch
```

## MCP (Model Context Protocol) Integration

:::tip[New: declarative install]
Use `apm install --mcp NAME` (or its alias `apm mcp install NAME`) to add servers from the command line in one step. See the [MCP Servers guide](../../guides/mcp-servers/) for the full workflow. This page covers per-IDE config-file locations and runtime targeting.
:::

APM provides first-class support for MCP servers, including registry-based servers that publish stdio packages (npm, pypi, docker) or HTTP/SSE remote endpoints.

### Auto-Discovery from Packages

APM auto-discovers MCP server declarations from packages during `apm install`:

- **apm.yml dependencies**: MCP servers listed under `dependencies.mcp` in a package's `apm.yml` are collected automatically.
- **plugin.json**: Packages with a `plugin.json` (at the root, `.github/plugin/`, or `.claude-plugin/`) are recognized as marketplace plugins. APM synthesizes an `apm.yml` from `plugin.json` metadata when no `apm.yml` exists. When both files are present (hybrid mode), APM uses `apm.yml` for dependency management while preserving `plugin.json` for plugin ecosystem compatibility. See [Plugin authoring](../../guides/plugins/#plugin-authoring).
- **Transitive collection**: APM walks the dependency tree and collects MCP servers from all transitive packages.

### Trust Model

APM enforces a trust boundary for MCP servers to prevent packages from silently injecting arbitrary server processes:

| Dependency Type | Registry Servers | Self-Defined Servers |
|----------------|-----------------|---------------------|
| Direct (depth 1) | Auto-trusted | Auto-trusted |
| Transitive (depth > 1) | Auto-trusted | Skipped with warning |

**Self-defined servers** are those declared with `registry: false` in `apm.yml` -- they run arbitrary commands rather than resolving through the official MCP registry.

To trust self-defined servers from transitive dependencies, either:
1. Re-declare the server in your root `apm.yml` (recommended), or
2. Use the `--trust-transitive-mcp` flag:

```bash
# Trust self-defined MCP servers from transitive packages
apm install --trust-transitive-mcp
```

### Client Configuration

APM configures MCP servers in the native config format for each supported client:

| Client | Config Location | Format |
|--------|----------------|--------|
| VS Code | `.vscode/mcp.json` | JSON `servers` object |
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` | JSON `mcpServers` object |
| Codex CLI | `~/.codex/config.toml` | TOML `mcp_servers` section |

**Runtime targeting**: APM detects which runtimes are installed and configures MCP servers for all of them. Use `--runtime <name>` or `--exclude <name>` to control which clients receive configuration.

> **VS Code detection**: APM considers VS Code available when either the `code` CLI command is on PATH **or** a `.vscode/` directory exists in the current working directory. This means VS Code MCP configuration works even when `code` is not on PATH — common on macOS and Linux when "Install 'code' command in PATH" has not been run from the VS Code command palette, or when VS Code was installed via a method that doesn't register the CLI (e.g. `.tar.gz`, Flatpak, or a non-standard macOS install location).

```bash
# Install MCP dependencies for all detected runtimes
apm install

# Target only VS Code
apm install --runtime vscode

# Skip Codex configuration
apm install --exclude codex

# Install only MCP dependencies (skip APM packages)
apm install --only mcp

# Preview MCP configuration without writing
apm install --dry-run
```

APM also handles stale server cleanup: when a package is uninstalled or an MCP dependency is removed, APM removes the corresponding entries from all client configs.

### Package Type Inference

The MCP registry API may return empty `registry_name` fields for packages. APM infers the package type from:

1. Explicit `registry_name` (when provided)
2. `runtime_hint` (e.g. `npx` to npm, `uvx` to pypi)
3. Package name patterns (e.g. `@scope/name` to npm, `ghcr.io/...` to docker, `PascalCase.Name` to nuget)

### Supported Package Types

When installing registry MCP servers, APM selects the best available package for each runtime:

| Package Registry | VS Code | Copilot CLI | Codex CLI |
|-----------------|---------|-------------|-----------|
| npm | Yes (npx) | Yes (npx) | Yes (npx) |
| pypi | Yes (uvx/python3) | Yes (uvx) | Yes (uvx) |
| docker | Yes | Yes | Yes |
| homebrew | -- | Yes | Yes |
| Other (with runtime_hint) | Yes (generic) | Yes (generic) | Yes (generic) |
| HTTP/SSE remotes | Yes | Yes | Yes |

### MCP Server Declaration

```yaml
# apm.yml - MCP dependencies
dependencies:
  mcp:
    # Simple registry references (resolved via MCP registry)
    - io.github.github/github-mcp-server
    - io.github.modelcontextprotocol/filesystem-server

    # Registry server with overlays
    - name: io.github.modelcontextprotocol/postgres-server
      transport: stdio
      package: npm
      args: ["--connection-string", "postgresql://localhost/mydb"]

    # Self-defined server (not in registry)
    - name: my-internal-server
      registry: false
      transport: stdio
      command: python
      args: ["-m", "my_server"]
      env:
        PORT: "3000"
```

```bash
# Install MCP dependencies
apm install

# Search the MCP registry
apm mcp search github

# Show server details
apm mcp show io.github.github/github-mcp-server

# List available MCP servers
apm mcp list
```

#### `${input:...}` Variables in `headers` and `env`

Values in `headers` and `env` can reference VS Code input variables using `${input:<variable-id>}`. At runtime, VS Code prompts the user for each referenced input before starting the server.

For registry-backed servers, APM auto-generates input prompts from registry metadata. For self-defined servers, APM detects the `${input:...}` patterns in your `apm.yml` and generates matching input definitions.

```yaml
dependencies:
  mcp:
    - name: my-server
      registry: false
      transport: http
      url: https://my-server.example.com/mcp/
      headers:
        Authorization: "Bearer ${input:my-server-token}"
        X-Project: "${input:my-server-project}"
```

**Runtime support:**

| Runtime | `${input:...}` support |
|---------|----------------------|
| VS Code | Yes -- prompts user at runtime |
| Copilot CLI | No -- use environment variables instead |
| Codex | No -- use environment variables instead |

## Roadmap

The following IDE integrations are planned for future releases:

- **JetBrains IDE support**: Native integration with IntelliJ, PyCharm, WebStorm, and other JetBrains IDEs
- **Windsurf support**: Integration with the Windsurf AI coding environment
- **Cursor deeper integration**: Enhanced Cursor support including rule versioning and conflict resolution

## Related Resources

- **[Getting Started](../../getting-started/installation/)** -- Set up APM in your environment
- **[Key Concepts](../../introduction/key-concepts/)** -- Core APM concepts and terminology
- **[CLI Reference](../../reference/cli-commands/)** -- Complete command documentation
- Review the [VSCode Copilot Customization Guide](https://code.visualstudio.com/docs/copilot/copilot-customization) for VSCode-specific features
- Check the [Spec-kit documentation](https://github.com/github/spec-kit) for SDD integration details
- Explore [MCP servers](https://modelcontextprotocol.io/servers) for tool integration options