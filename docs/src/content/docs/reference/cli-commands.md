---
title: "CLI Commands"
sidebar:
  order: 1
---

Complete reference for all APM CLI commands and options.

:::tip[New to APM?]
See [Installation](../../getting-started/installation/) and [Quick Start](../../getting-started/quick-start/) to get up and running.
:::

## Global Options

```bash
apm [OPTIONS] COMMAND [ARGS]...
```

### Options
- `--version` - Show version and exit
- `--help` - Show help message and exit

## Core Commands

### `apm init` - Initialize new APM project

Initialize a new APM project with minimal `apm.yml` configuration (like `npm init`).

```bash
apm init [PROJECT_NAME] [OPTIONS]
```

**Arguments:**
- `PROJECT_NAME` - Optional name for new project directory. Use `.` to explicitly initialize in current directory

**Options:**
- `-y, --yes` - Skip interactive prompts and use auto-detected defaults
- `--plugin` - Initialize as a plugin authoring project (creates `plugin.json` + `apm.yml` with `devDependencies`)
- `--marketplace` - Seed `apm.yml` with a `marketplace:` authoring block. See the [Authoring a marketplace guide](../../guides/marketplace-authoring/).

**Examples:**
```bash
# Initialize in current directory (interactive)
apm init

# Initialize in current directory with defaults
apm init --yes

# Create new project directory
apm init my-hello-world

# Create project with auto-detected defaults
apm init my-project --yes

# Initialize a plugin authoring project
apm init my-plugin --plugin

# Initialize a project that also publishes a marketplace
apm init my-marketplace --marketplace
```

**Behavior:**
- **Minimal by default**: Creates only `apm.yml` with auto-detected metadata
- **Interactive mode**: Prompts for project details unless `--yes` specified
- **Auto-detection**: Automatically detects author from `git config user.name` and description from project context
- **Brownfield friendly**: Works cleanly in existing projects without file pollution
- **Plugin mode** (`--plugin`): Creates both `plugin.json` and `apm.yml` with an empty `devDependencies` section. Plugin names must be kebab-case (`^[a-z][a-z0-9-]{0,63}$`), max 64 characters

**Creates:**
- `apm.yml` - Minimal project configuration with empty dependencies and scripts sections
- `plugin.json` - Plugin manifest (only with `--plugin`)

**Auto-detected fields:**
- `name` - From project directory name
- `author` - From `git config user.name` (fallback: "Developer")
- `description` - Generated from project name
- `version` - Defaults to "1.0.0"

### `apm install` - Install dependencies and deploy local content

Install APM package and MCP server dependencies from `apm.yml` and deploy the project's own `.apm/` content to target directories (like `npm install`). Auto-creates minimal `apm.yml` when packages are specified but no manifest exists. For `http://` dependencies, use `--allow-insecure`.

```bash
apm install [PACKAGES...] [OPTIONS]
```

**Arguments:**
- `PACKAGES` - Optional APM packages to add and install. Accepts shorthand (`owner/repo`), HTTPS URLs, SSH URLs, FQDN shorthand (`host/owner/repo`), local filesystem paths (`./path`, `../path`, `/absolute/path`, `~/path`), or marketplace references (`NAME@MARKETPLACE[#ref]`). All forms are normalized to canonical format in `apm.yml`.

**Options:**
- `--runtime TEXT` - Target specific runtime only (copilot, codex, gemini, vscode)
- `--exclude TEXT` - Exclude specific runtime from installation
- `--only [apm|mcp]` - Install only specific dependency type
- `--target [copilot|claude|cursor|codex|opencode|gemini|copilot-cowork|all]` - Force deployment to specific target(s). Accepts comma-separated values for multiple targets (e.g., `-t claude,copilot`). Overrides auto-detection
  - `copilot-cowork` - Microsoft 365 Copilot Cowork skills (user scope only, requires `copilot-cowork` experimental flag)
- `--update` - Update dependencies to latest Git references  
- `--force` - Overwrite locally-authored files on collision; bypass security scan blocks
- `--dry-run` - Show what would be installed without installing
- `--parallel-downloads INTEGER` - Max concurrent package downloads (default: 4, 0 to disable)
- `--verbose` - Show individual file paths and full error details in the diagnostic summary
- `--trust-transitive-mcp` - Trust self-defined MCP servers from transitive packages (skip re-declaration requirement)
- `--mcp NAME` - Add an MCP server entry to `apm.yml` and install it. See the [MCP Servers guide](../../guides/mcp-servers/) for the full workflow.
- `--transport [stdio|http|sse|streamable-http]` - MCP transport (only with `--mcp`). Inferred from `--url` or post-`--` argv when omitted.
- `--url URL` - Endpoint for `http`/`sse` MCP servers (only with `--mcp`). Scheme must be `http` or `https`.
- `--env KEY=VALUE` - Environment variable for stdio MCP servers (only with `--mcp`). Repeatable.
- `--header KEY=VALUE` - HTTP header for remote MCP servers (only with `--mcp`). Repeatable. Requires `--url`.
- `--mcp-version VER` - Pin a registry MCP entry to a specific version (only with `--mcp`).
- `--registry URL` - Custom MCP registry URL (`http://` or `https://`) for resolving the registry-form `--mcp NAME`. Overrides `MCP_REGISTRY_URL`. Persisted to `apm.yml` for reproducible installs. Not valid with `--url` or a stdio command. Only with `--mcp`.
- `--dev` - Add packages to [`devDependencies`](../manifest-schema/#5-devdependencies) instead of `dependencies`. Dev deps are installed locally but excluded from `apm pack --format plugin` bundles
- `-g, --global` - Install to user scope (`~/.apm/`) instead of the current project. Primitives deploy to `~/.copilot/`, `~/.claude/`, etc. MCP servers are only installed for global-capable runtimes (Copilot CLI, Codex CLI); workspace-only runtimes are skipped.
- `--allow-insecure` - Allow HTTP (insecure) dependencies. Required when adding or installing dependencies that use an `http://` URL.
- `--allow-insecure-host HOSTNAME` - Allow transitive HTTP (insecure) dependencies from `HOSTNAME`. Repeat the flag to allow multiple hosts.
- `--ssh` - Force SSH for shorthand (`owner/repo`) dependencies. Mutually exclusive with `--https`. Ignored for URLs with an explicit scheme.
- `--https` - Force HTTPS for shorthand dependencies. Mutually exclusive with `--ssh`. Default unless `git config url.<base>.insteadOf` rewrites the candidate to SSH.
- `--allow-protocol-fallback` - Restore the legacy permissive cross-protocol fallback chain (HTTPS-then-SSH or vice-versa). Strict-by-default otherwise. Each retry emits a `[!]` warning naming both protocols. When the dependency URL carries a custom port, APM also emits a one-shot `[!]` warning before the first clone attempt noting that the same port will be reused across schemes (wrong on servers like Bitbucket Datacenter that serve SSH and HTTPS on different ports) -- to avoid the mismatch, omit this flag and pin the dependency with an explicit `ssh://` or `https://` URL.
- `--no-policy` -- Skip org policy enforcement for this invocation. Loudly logged. Does NOT bypass `apm audit --ci`. Available on `apm install`, `apm install <pkg>`, and `apm install --mcp <name>`.
  - Equivalent env var: `APM_POLICY_DISABLE=1` (applies to the entire shell session). Note: `apm deps update` runs the install pipeline and is gated by policy but does not currently expose a `--no-policy` flag -- use `APM_POLICY_DISABLE=1` as the only escape hatch there.
- `--skill NAME` - Install only named skill(s) from a `SKILL_BUNDLE` package. Repeatable. The selection is **persisted** in `apm.yml` (as a `skills:` list in dict-form entries) and in `apm.lock.yaml` (as `skill_subset`), so subsequent bare `apm install` commands are deterministic. Use `--skill '*'` to reset and install all skills from the bundle.

**Transport env vars:**

| Variable | Purpose |
|----------|---------|
| `APM_GIT_PROTOCOL` | `ssh` or `https`. Default initial transport for shorthand dependencies (overridden by `--ssh` / `--https`). |
| `APM_ALLOW_PROTOCOL_FALLBACK` | Set to `1` to enable the legacy permissive chain without passing `--allow-protocol-fallback`. |

See [Dependencies: Transport selection](../../guides/dependencies/#transport-selection-ssh-vs-https) for the full selection matrix.

**Behavior:**
- `apm install` (no args): Installs **all** packages from `apm.yml` and deploys the project's own `.apm/` content
- `apm install <package>`: Installs **only** the specified package (adds to `apm.yml` if not present)
- Each `http://` dependency is warned at install time before any fetch begins
- Transitive `http://` dependencies are allowed automatically when they use the same host as a direct insecure dependency you approved with `--allow-insecure`; other transitive hosts require `--allow-insecure-host HOSTNAME`

**Local `.apm/` Content Deployment:**

After integrating dependencies, `apm install` deploys primitives from the project's own `.apm/` directory (instructions, prompts, agents, skills, hooks, commands) to target directories (`.github/`, `.claude/`, `.cursor/`, etc.). Local content takes priority over dependencies on collision. Deployed files are tracked in the lockfile for cleanup on subsequent installs. This works even with zero dependencies -- just `apm.yml` and `.apm/` content is enough.

Exceptions:
- Skipped at user scope (`--global`)
- Skipped with `--only=mcp`
- Root `SKILL.md` is not deployed as a local skill (it describes the project itself)

**Diff-Aware Installation (manifest as source of truth):**
- MCP servers already configured with matching config are skipped (`already configured`)
- MCP servers already configured but with changed manifest config are re-applied automatically (`updated`)
- APM packages removed from `apm.yml` have their deployed files cleaned up on the next full `apm install`
- APM packages whose ref/version changed in `apm.yml` are re-downloaded automatically (no `--update` needed)
- `--force` remains available for full overwrite/reset scenarios

**Stale-file cleanup:**

`apm install` removes files that a still-present package previously deployed but no longer produces -- for example after a package renames or drops a primitive. This keeps the workspace consistent with the manifest without any manual `apm prune`/`uninstall` step. Behaviour:

- Scope: only files recorded under that package's `deployed_files` in `apm.lock.yaml` are eligible
- Safety gate: paths that escape the project root or fall outside known integration prefixes are refused
- Directory entries are refused outright -- APM only deletes individual files
- Per-file provenance: APM records a content hash for each deployed file; if the on-disk content has changed since deploy time the file is treated as user-edited and kept (with a warning explaining how to remove it manually)
- Skipped when integration reports an error for the package (avoids deleting a file that just failed to redeploy)
- Files that fail to delete are kept in `deployed_files` and retried on the next `apm install`
- Use `apm install --dry-run` to preview package-level orphan cleanup; intra-package stale cleanup is not previewed because it requires running integration

**Examples:**
```bash
# Install all dependencies from apm.yml
apm install

# Install ONLY this package (not others in apm.yml)
apm install microsoft/apm-sample-package

# Install via HTTPS URL (normalized to owner/repo in apm.yml)
apm install https://github.com/microsoft/apm-sample-package.git

# Install from a non-GitHub host (FQDN preserved)
apm install https://gitlab.com/acme/coding-standards.git

# Add multiple packages and install
apm install org/pkg1 org/pkg2

# Install a Claude Skill from a subdirectory
apm install ComposioHQ/awesome-claude-skills/brand-guidelines

# Install only APM dependencies (skip MCP servers)
apm install --only=apm

# Install only MCP dependencies (skip APM packages)  
apm install --only=mcp

# Preview what would be installed
apm install --dry-run

# Update existing dependencies to latest versions
apm install --update

# Install for all runtimes except Codex
apm install --exclude codex

# Trust self-defined MCP servers from transitive packages
apm install --trust-transitive-mcp

# Add an MCP server in one shot (writes apm.yml + wires every detected client)
apm install --mcp filesystem -- npx -y @modelcontextprotocol/server-filesystem /workspace
apm install --mcp io.github.github/github-mcp-server

# Install as a dev dependency (excluded from plugin bundles)
apm install --dev owner/test-helpers

# Install from a local path (copies to apm_modules/_local/)
apm install ./packages/my-shared-skills
apm install /home/user/repos/my-ai-package

# Install to user scope (available across all projects)
apm install -g microsoft/apm-sample-package

# Install a plugin from a registered marketplace
apm install code-review@acme-plugins

# Install a specific ref from a marketplace
apm install code-review@acme-plugins#v2.0.0
```

**Auto-Bootstrap Behavior:**
- **With packages + no apm.yml**: Automatically creates minimal `apm.yml`, adds packages, and installs
- **Without packages + no apm.yml**: Shows helpful error suggesting `apm init` or `apm install <org/repo>`
- **With apm.yml**: Works as before - installs existing dependencies or adds new packages

**Dependency Types:**

- **APM Dependencies**: Git repositories containing `apm.yml` (GitHub, GitLab, Bitbucket, or any git host)
- **Claude Skills**: Repositories with `SKILL.md` (auto-generates `apm.yml` upon installation)
  - Example: `apm install ComposioHQ/awesome-claude-skills/brand-guidelines`
  - Skills are transformed to `.github/agents/*.agent.md` for VSCode target
- **Hook Packages**: Repositories with `hooks/*.json` (no `apm.yml` or `SKILL.md` required)
  - Example: `apm install anthropics/claude-plugins-official/plugins/hookify`
- **Virtual Packages**: Single files or collections installed directly from URLs
  - Single `.prompt.md` or `.agent.md` files from any GitHub repository
  - Collections from curated sources (e.g., `github/awesome-copilot`)
  - Example: `apm install github/awesome-copilot/skills/review-and-refactor`
- **MCP Dependencies**: Model Context Protocol servers for runtime integration

**Working Example with Dependencies:**
```yaml
# Example apm.yml with APM dependencies
name: my-compliance-project
version: 1.0.0
dependencies:
  apm:
    - microsoft/apm-sample-package  # Design standards, prompts
    - github/awesome-copilot/skills/review-and-refactor  # Code review skill
  mcp:
    - io.github.github/github-mcp-server
```

```bash
# Install all dependencies (APM + MCP)
apm install

# Install only APM dependencies for faster setup
apm install --only=apm

# Preview what would be installed  
apm install --dry-run
```

**Auto-Detection:**

APM automatically detects which integrations to enable based on your project structure:

- **VSCode integration**: Enabled when `.github/` directory exists
- **Claude integration**: Enabled when `.claude/` directory exists
- **Cursor integration**: Enabled when `.cursor/` directory exists
- **OpenCode integration**: Enabled when `.opencode/` directory exists
- **Codex integration**: Enabled when `.codex/` directory exists
- **Gemini integration**: Enabled when `.gemini/` directory exists
- All integrations can coexist in the same project

**VSCode Integration (`.github/` present):**

When you run `apm install`, APM automatically integrates primitives from installed packages and the project's own `.apm/` directory:

- **Prompts**: `.prompt.md` files → `.github/prompts/*.prompt.md`
- **Agents**: `.agent.md` files → `.github/agents/*.agent.md`
- **Chatmodes**: `.chatmode.md` files → `.github/agents/*.agent.md` (renamed to modern format)
- **Instructions**: `.instructions.md` files → `.github/instructions/*.instructions.md`
- **Control**: Disable with `apm config set auto-integrate false`
- **Smart updates**: Only updates when package version/commit changes
- **Hooks**: Hook `.json` files → `.github/hooks/*.json` with scripts bundled
- **Collision detection**: Skips local files that aren't managed by APM; use `--force` to overwrite
- **Security scanning**: Source files are scanned for hidden Unicode characters before deployment. Critical findings (tag characters, bidi overrides) block deployment; use `--force` to override. Exits with code 1 if any package was blocked.

**Diagnostic Summary:**

After installation completes, APM prints a grouped diagnostic summary instead of inline warnings. Categories include collisions (skipped files), cross-package skill replacements, warnings, and errors.

- **Normal mode**: Shows counts and actionable tips (e.g., "9 files skipped -- use `apm install --force` to overwrite")
- **Verbose mode** (`--verbose`): Additionally lists individual file paths grouped by package, full error details, and **the resolved auth source per remote host** (e.g., `[i] dev.azure.com -- using bearer from az cli (source: AAD_BEARER_AZ_CLI)` or `[i] github.com -- token from GITHUB_APM_PAT`). Useful for diagnosing PAT vs. Entra-ID-bearer behaviour against Azure DevOps.

```bash
# See exactly which files were skipped or had issues, and which auth source was used
apm install --verbose
```

**Claude Integration (`.claude/` present):**

APM also integrates with Claude Code when `.claude/` directory exists:

- **Agents**: `.agent.md` and `.chatmode.md` files → `.claude/agents/*.md`
- **Commands**: `.prompt.md` files → `.claude/commands/*.md`
- **Hooks**: Hook definitions merged into `.claude/settings.json` hooks key

**Skill Integration:**

Skills are copied directly to target directories:

- **Primary**: `.github/skills/{skill-name}/` — Entire skill folder copied
- **Compatibility**: `.claude/skills/{skill-name}/` — Also copied if `.claude/` folder exists

**Example Integration Output**:
```
✓ microsoft/apm-sample-package
  ├─ 3 prompts integrated → .github/prompts/
  ├─ 1 instruction(s) integrated → .github/instructions/
  ├─ 1 agents integrated → .claude/agents/
  └─ 3 commands integrated → .claude/commands/
```

This makes all package primitives available in VSCode, Cursor, OpenCode, Claude Code, and compatible editors for immediate use with your coding agents.

### `apm uninstall` - Remove APM packages

Remove installed APM packages and their integrated files.

```bash
apm uninstall [OPTIONS] PACKAGES...
```

**Arguments:**
- `PACKAGES...` - One or more packages to uninstall. Accepts any format — shorthand (`owner/repo`), HTTPS URL, SSH URL, or FQDN. APM resolves each to the canonical identity stored in `apm.yml`.

**Options:**
- `--dry-run` - Show what would be removed without removing
- `-v, --verbose` - Show detailed removal information
- `-g, --global` - Remove from user scope (`~/.apm/`) instead of the current project

**Examples:**
```bash
# Uninstall a package
apm uninstall microsoft/apm-sample-package

# Uninstall using an HTTPS URL (resolves to same identity)
apm uninstall https://github.com/microsoft/apm-sample-package.git

# Preview what would be removed
apm uninstall microsoft/apm-sample-package --dry-run

# Uninstall from user scope
apm uninstall -g microsoft/apm-sample-package
```

**What Gets Removed:**

| Item | Location |
|------|----------|
| Package entry | `apm.yml` dependencies section |
| Package folder | `apm_modules/owner/repo/` |
| Transitive deps | `apm_modules/` (orphaned transitive dependencies) |
| Integrated prompts | `.github/prompts/*.prompt.md` |
| Integrated agents | `.github/agents/*.agent.md` |
| Integrated chatmodes | `.github/agents/*.agent.md` |
| Claude commands | `.claude/commands/*.md` |
| Skill folders | `.github/skills/{folder-name}/` |
| Integrated hooks | `.github/hooks/*.json` |
| Claude hook settings | `.claude/settings.json` (hooks key cleaned) |
| Cursor rules | `.cursor/rules/*.mdc` |
| Cursor agents | `.cursor/agents/*.md` |
| Cursor skills | `.cursor/skills/{folder-name}/` |
| Cursor hooks | `.cursor/hooks.json` (hooks key cleaned) |
| OpenCode agents | `.opencode/agents/*.md` |
| OpenCode commands | `.opencode/commands/*.md` |
| OpenCode skills | `.opencode/skills/{folder-name}/` |
| Gemini commands | `.gemini/commands/*.toml` |
| Gemini skills | `.gemini/skills/{folder-name}/` |
| Gemini settings | `.gemini/settings.json` (hooks + MCP cleaned) |
| Lockfile entries | `apm.lock.yaml` (removed packages + orphaned transitives) |

**Behavior:**
- Removes package from `apm.yml` dependencies
- Deletes package folder from `apm_modules/`
- Removes orphaned transitive dependencies (npm-style pruning via `apm.lock.yaml`)
- Removes all deployed integration files tracked in `apm.lock.yaml` `deployed_files`
- Updates `apm.lock.yaml` (or deletes it if no dependencies remain)
- Cleans up empty parent directories
- Safe operation: only removes files tracked in the `deployed_files` manifest

### `apm prune` - Remove orphaned packages

Remove APM packages from `apm_modules/` that are not listed in `apm.yml`, along with their deployed integration files (prompts, agents, hooks, etc.).

```bash
apm prune [OPTIONS]
```

**Options:**
- `--dry-run` - Show what would be removed without removing

**Examples:**
```bash
# Remove orphaned packages and their deployed files
apm prune

# Preview what would be removed
apm prune --dry-run
```

**Behavior:**
- Removes orphaned package directories from `apm_modules/`
- Removes deployed integration files (prompts, agents, hooks, etc.) for pruned packages using the `deployed_files` manifest in `apm.lock.yaml`
- Updates `apm.lock.yaml` to reflect the pruned state

### `apm audit` - Scan for hidden Unicode characters

Scan installed packages or arbitrary files for hidden Unicode characters that could embed invisible instructions in prompt files.

```bash
apm audit [PACKAGE] [OPTIONS]
```

**Arguments:**
- `PACKAGE` - Optional package key to scan (repo URL from lockfile). If omitted, scans all installed packages.

**Options:**
- `--file PATH` - Scan an arbitrary file instead of installed packages
- `--strip` - Remove dangerous characters (critical + warning severity) while preserving info-level content like emoji. ZWJ inside emoji sequences is preserved.
- `--dry-run` - Preview what `--strip` would remove without modifying files
- `-v, --verbose` - Show info-level findings and file details
- `-f, --format [text|json|sarif|markdown]` - Output format: `text` (default), `json` (machine-readable), `sarif` (GitHub Code Scanning), `markdown` (step summaries). Cannot be combined with `--strip` or `--dry-run`.
- `-o, --output PATH` - Write report to file. Auto-detects format from extension (`.sarif`, `.sarif.json` → SARIF; `.json` → JSON; `.md` → Markdown) when `--format` is not specified.
- `--ci` - Run lockfile consistency checks for CI/CD gates. Exit 0 if clean, 1 if violations found. Auto-discovers org policy from the org `.github` repo unless `--no-policy` is set. Runs the 7 baseline checks: lockfile presence, ref consistency, deployed files present, no orphaned packages, MCP config consistency, content integrity (Unicode + hash drift on every deployed file including local content), includes consent (advisory).
- `--policy SOURCE` - *(Experimental)* Policy source. Accepts: `org` (auto-discover from your project's git remote), `owner/repo` (defaults to github.com), an `https://` URL, or a local file path. Used with `--ci` for policy checks. Without this flag, `--ci` auto-discovers.
- `--no-policy` - Skip policy discovery and enforcement entirely. Equivalent to `APM_POLICY_DISABLE=1`.
- `--no-cache` - Force fresh policy fetch (skip cache). Only relevant with policy discovery active.
- `--no-fail-fast` - Run all checks even after a failure. By default, CI mode stops at the first failing check to save time.

**Examples:**
```bash
# Scan all installed packages
apm audit

# Scan a specific package
apm audit https://github.com/owner/repo

# Scan any file (even non-APM-managed)
apm audit --file .cursorrules

# Remove dangerous characters (preserves emoji)
apm audit --strip

# Preview what --strip would remove
apm audit --strip --dry-run

# Verbose output with info-level findings
apm audit --verbose

# SARIF output to stdout (for CI pipelines)
apm audit -f sarif

# Markdown output (for GitHub step summaries)
apm audit -f markdown

# Write SARIF report to file
apm audit -o report.sarif

# JSON report to file
apm audit -f json -o results.json

# CI lockfile consistency gate (auto-discovers org policy)
apm audit --ci

# CI gate skipping policy discovery (baseline checks only)
apm audit --ci --no-policy

# CI gate with explicit policy source (overrides auto-discovery)
apm audit --ci --policy org

# CI gate with local policy file
apm audit --ci --policy ./apm-policy.yml

# Force fresh policy fetch
apm audit --ci --no-cache

# Run all checks (no fail-fast) for full diagnostic report
apm audit --ci --policy org --no-fail-fast
```

**Exit codes (content scanning mode):**
| Code | Meaning |
|------|---------|
| 0 | Clean — no findings, info-only, or successful strip |
| 1 | Critical findings — tag characters, bidi overrides, or variation selectors 17–256 |
| 2 | Warnings only — zero-width characters, bidi marks, or other suspicious content |

**Exit codes (`--ci` mode):**
| Code | Meaning |
|------|---------|
| 0 | All checks passed |
| 1 | One or more checks failed |

**What it detects:**
- **Critical**: Tag characters (U+E0001–E007F), bidi overrides (U+202A–E, U+2066–9), variation selectors 17–256 (U+E0100–E01EF, Glassworm attack vector)
- **Warning**: Zero-width spaces/joiners (U+200B–D), variation selectors 1–15 (U+FE00–FE0E), bidi marks (U+200E–F, U+061C), invisible operators (U+2061–4), annotation markers (U+FFF9–B), deprecated formatting (U+206A–F), soft hyphen (U+00AD), mid-file BOM
- **Info**: Non-breaking spaces, unusual whitespace, emoji presentation selector (U+FE0F). ZWJ between emoji characters is context-downgraded to info.
- **Hash drift (`--ci` only)**: Files deployed by `apm install` whose on-disk SHA-256 no longer matches the value recorded in the lockfile (`deployed_file_hashes`). Covers content from package dependencies AND local `.apm/` content via the synthesized self-entry.

### `apm policy` - Inspect organization policy

Diagnostic commands for the organization-level `apm-policy.yml` resolved by APM at install / audit time. See [Policy Reference](../../enterprise/policy-reference/) for the full schema and enforcement model.

#### `apm policy status` - Show resolved policy state

Show what policy APM resolved for the current project: discovery outcome, source, enforcement level, cache age, `extends:` chain, and effective rule counts. Trust-but-verify diagnostic for admins and CI gates.

```bash
apm policy status [OPTIONS]
```

**Options:**
- `--policy-source SOURCE` - Override discovery. Accepts: `org` (auto-discover from your project's git remote), `owner/repo` (defaults to github.com), an `https://` URL, or a local file path.
- `--no-cache` - Force fresh fetch (skip cache).
- `--json` / `-o json` - Machine-readable output for SIEM ingestion or CI inspection.
- `--check` - Exit non-zero (1) when no usable policy is found. Default is always 0; use `--check` for CI pre-checks.

**Exit codes:**

| Mode | `outcome=found` | Anything else (absent, error, disabled, ...) |
|------|-----------------|-----------------------------------------------|
| default | 0 | 0 |
| `--check` | 0 | 1 |

The default is exit-0 so the command is safe for human and SIEM use; `--check` opts into a CI-friendly contract similar to `npm audit` / `pip check`. To gate on policy compliance (rule violations) instead of resolvability, use `apm audit --ci`.

**Examples:**
```bash
# Show resolved org policy state
apm policy status

# Force fresh fetch (bypass cache)
apm policy status --no-cache

# Machine-readable JSON for SIEM
apm policy status --json

# Inspect a specific policy without committing it
apm policy status --policy-source ./draft-policy.yml

# CI gate: fail the job if no usable policy is resolved
apm policy status --check
```

### `apm pack` - Pack distributable artifacts

Pack distributable artifacts from your APM project. The manifest drives what gets produced:

- `dependencies:` block in `apm.yml` -> bundle (directory or `.tar.gz`)
- `marketplace:` block in `apm.yml` -> `.claude-plugin/marketplace.json`
- both blocks present -> both artifacts in a single run

The lockfile (`apm.lock.yaml`) pins bundle contents. An enriched copy is embedded in each bundle.

```bash
apm pack [OPTIONS]
```

**Options:**
- `-o, --output PATH` - Bundle output directory (default: `./build`). Does not affect `marketplace.json` path.
- `-t, --target [copilot|vscode|claude|cursor|codex|opencode|gemini|all]` - Filter bundle files by target. Accepts comma-separated values (e.g., `-t claude,copilot`). Auto-detects from `apm.yml` if omitted. `vscode` is an alias for `copilot`. No-op for marketplace output.
- `--archive` - Produce a `.tar.gz` archive instead of a directory. Bundle only.
- `--format [apm|plugin]` - Bundle format (default: `apm`). `plugin` produces a standalone plugin directory with `plugin.json`. No-op for marketplace output.
- `--force` - On collision (plugin format), last writer wins instead of first. Bundle only.
- `--dry-run` - Preview outputs without writing anything.
- `--offline` - Marketplace: use cached refs only (skip `git ls-remote`).
- `--include-prerelease` - Marketplace: allow pre-release tags to satisfy version ranges.
- `--marketplace-output PATH` - Marketplace: override the output path (default: `.claude-plugin/marketplace.json`).
- `-v, --verbose` - Detailed output from every producer.

Flags whose scope does not match the detected outputs are silent no-ops, not errors. CI scripts can pass `--offline` unconditionally even when some projects only produce a bundle.

**Exit codes:**
- `0` - Success
- `1` - Build or runtime error (network failure, ref not found, no tag matches a range, etc.)
- `2` - Schema validation error in `apm.yml`

**Examples:**
```bash
# Bundle only (apm.yml has dependencies:, no marketplace:)
apm pack
apm pack --target claude --archive
apm pack --format plugin -o ./dist

# Marketplace only (apm.yml has marketplace:, no dependencies:)
apm pack
apm pack --offline --dry-run

# Both blocks present -- one command, both artifacts
apm pack
apm pack --archive --offline

# Override marketplace.json path (rare; default matches Anthropic spec)
apm pack --marketplace-output ./build/marketplace.json
```

**Bundle behaviour:**
- Reads `apm.lock.yaml` to enumerate all `deployed_files` from installed dependencies
- Scans files for hidden Unicode characters before bundling — warns if findings are detected (non-blocking; consumers are protected by `apm install`/`apm unpack` which block on critical)
- Copies files preserving directory structure
- Writes an enriched `apm.lock.yaml` inside the bundle with a `pack:` metadata section (the project's own `apm.lock.yaml` is never modified)
- **Plugin format** (`--format plugin`): Remaps `.apm/` content into plugin-native paths (`agents/`, `skills/`, `commands/`, etc.), generates or updates `plugin.json`, merges hooks into a single `hooks.json`. `devDependencies` are also excluded from plugin bundles. See [Pack & Distribute](../../guides/pack-distribute/#plugin-format) for the full mapping table

**Marketplace behaviour:**
- Reads the `marketplace:` block from `apm.yml` (falls back to legacy `marketplace.yml` with a deprecation warning when no block is present; both files present is a hard error)
- Resolves each remote plugin's version range against `git ls-remote`; emits local-path entries verbatim
- Writes `.claude-plugin/marketplace.json` atomically -- this is where Claude Code reads the file from the repo root
- Creates `.claude-plugin/` if absent; never scaffolds other files there
- See the [Authoring a marketplace guide](../../guides/marketplace-authoring/) for the full schema and workflow

**Bundle target filtering:**

| Target | Includes paths starting with |
|--------|------------------------------|
| `vscode` | `.github/` |
| `claude` | `.claude/` |
| `cursor` | `.cursor/` |
| `opencode` | `.opencode/` |
| `gemini` | `.gemini/` |
| `all` | all of the above |

**Enriched lockfile example:**
```yaml
pack:
  format: apm
  target: vscode
  packed_at: '2026-03-09T12:00:00+00:00'
lockfile_version: '1'
generated_at: ...
dependencies:
  - repo_url: owner/repo
    ...
```

### `apm unpack` - Extract a bundle

Extract an APM bundle into the current project with optional completeness verification.

```bash
apm unpack BUNDLE_PATH [OPTIONS]
```

**Arguments:**
- `BUNDLE_PATH` - Path to a `.tar.gz` archive or an unpacked bundle directory

**Options:**
- `-o, --output PATH` - Target project directory (default: current directory)
- `--skip-verify` - Skip completeness verification against the bundle lockfile
- `--force` - Deploy despite critical hidden-character findings
- `--dry-run` - Show what would be extracted without writing anything

**Examples:**
```bash
# Unpack an archive into the current directory
apm unpack ./build/my-pkg-1.0.0.tar.gz

# Unpack into a specific directory
apm unpack bundle.tar.gz --output /path/to/project

# Skip verification (useful for partial bundles)
apm unpack bundle.tar.gz --skip-verify

# Preview what would be extracted
apm unpack bundle.tar.gz --dry-run

# Deploy despite critical hidden-character findings
apm unpack bundle.tar.gz --force
```

**Behavior:**
- **Additive-only**: only writes files listed in the bundle's `apm.lock.yaml`; never deletes existing files
- If a local file has the same path as a bundle file, the bundle file wins (overwrite)
- **Security scanning**: Bundle contents are scanned before deployment. Critical findings block deployment unless `--force` is used (exit code 1)
- Verification checks that all `deployed_files` from the bundle lockfile are present in the bundle
- The bundle's `apm.lock.yaml` is metadata only — it is **not** copied to the output directory

### `apm update` - Update APM to the latest version

Update the APM CLI to the latest version available on GitHub releases.

```bash
apm update [OPTIONS]
```

**Options:**
- `--check` - Only check for updates without installing

**Examples:**
```bash
# Check if an update is available
apm update --check

# Update to the latest version
apm update
```

**Behavior:**
- Fetches latest release from GitHub
- Compares with current installed version
- Downloads and runs the official platform installer (`install.sh` on macOS/Linux, `install.ps1` on Windows)
- Preserves existing configuration and projects
- Shows progress and success/failure status
- Some package-manager distributions can disable self-update at build time. 
  In those builds, `apm update` prints a distributor-defined guidance message
  (for example, a `brew upgrade` command) and exits without running the installer.

**Version Checking:**
APM automatically checks for updates (at most once per day) when running any command. If a newer version is available, you'll see a yellow warning:

```
⚠️  A new version of APM is available: 0.7.0 (current: 0.6.3)
Run apm update to upgrade
```

This check is non-blocking and cached to avoid slowing down the CLI.

In distributions that disable self-update at build time, this startup update notification is skipped.

**Manual Update:**
If the automatic update fails, you can always update manually:

#### Linux / macOS
```bash
curl -sSL https://aka.ms/apm-unix | sh
```

#### Windows
```powershell
powershell -ExecutionPolicy Bypass -c "irm https://aka.ms/apm-windows | iex"
```

### `apm view` - View package metadata or list remote versions

Show local metadata for an installed package, or query remote refs with a field selector.

> **Note:** `apm info` is accepted as a hidden alias for backward compatibility.

```bash
apm view PACKAGE [FIELD] [OPTIONS]
```

**Arguments:**
- `PACKAGE` - Package name: `owner/repo`, short repo name, or `NAME@MARKETPLACE` for marketplace plugins
- `FIELD` - Optional field selector. Supported value: `versions`

**Options:**
- `-g, --global` - Inspect package from user scope (`~/.apm/`)

**Examples:**
```bash
# Show installed package metadata
apm view microsoft/apm-sample-package

# Short-name lookup for an installed package
apm view apm-sample-package

# List remote tags and branches without cloning
apm view microsoft/apm-sample-package versions

# View available versions for a marketplace plugin
apm view code-review@acme-plugins

# Inspect a package from user scope
apm view microsoft/apm-sample-package -g
```

**Behavior:**
- Without `FIELD`, reads installed package metadata from `apm_modules/`
- Shows package name, version, description, source, install path, context files, workflows, and hooks
- `versions` lists remote tags and branches without cloning the repository
- `versions` does not require the package to be installed locally
- `NAME@MARKETPLACE` syntax shows the marketplace plugin metadata (name, version, source, description, tags)

### `apm outdated` - Check locked dependencies for updates

Compare locked dependencies against remote refs to detect staleness.

```bash
apm outdated [OPTIONS]
```

**Options:**
- `-g, --global` - Check user-scope dependencies from `~/.apm/`
- `-v, --verbose` - Show extra detail for outdated packages, including available tags
- `-j, --parallel-checks N` - Max concurrent remote checks (default: 4, 0 = sequential)

**Examples:**
```bash
# Check project dependencies
apm outdated

# Check user-scope dependencies
apm outdated --global

# Show available tags for outdated packages
apm outdated --verbose

# Use 8 parallel checks for large dependency sets
apm outdated -j 8
```

**Behavior:**
- Reads the current lockfile (`apm.lock.yaml`; legacy `apm.lock` is migrated automatically)
- For tag-pinned deps: compares the locked semver tag against the latest available remote tag
- For branch-pinned deps: compares the locked commit SHA against the remote branch tip SHA
- For marketplace deps: compares the installed ref against the marketplace entry's current `source.ref`
- For deps with no ref: compares against the default branch (main/master) tip SHA
- Displays `Package`, `Current`, `Latest`, `Status`, and `Source` columns
- `Source` shows `marketplace: <name>` for marketplace-sourced deps
- Status values are `up-to-date`, `outdated`, and `unknown`
- Local dependencies and Artifactory dependencies are skipped

### `apm deps` - Manage APM package dependencies

Manage APM package dependencies with installation status, tree visualization, and package information.

```bash
apm deps COMMAND [OPTIONS]
```

#### `apm deps list` - List installed APM dependencies

Show all installed APM dependencies in a Rich table format with per-primitive counts.

```bash
apm deps list [OPTIONS]
```

**Options:**
- `-g, --global` - List user-scope packages from `~/.apm/` instead of the current project
- `--all` - List packages from both project and user scope
- `--insecure` - Show only installed dependencies locked to `http://` sources

**Examples:**
```bash
# Show project-scope packages
apm deps list

# Show user-scope packages
apm deps list -g

# Show both scopes
apm deps list --all

# Show only insecure installed dependencies
apm deps list --insecure
```

**Sample Output:**
```
┌─────────────────────┬─────────┬──────────┬─────────┬──────────────┬────────┬────────┐
│ Package             │ Version │ Source   │ Prompts │ Instructions │ Agents │ Skills │
├─────────────────────┼─────────┼──────────┼─────────┼──────────────┼────────┼────────┤
│ compliance-rules    │ 1.0.0   │ github   │    2    │      1       │   -    │   1    │
│ design-guidelines   │ 1.0.0   │ github   │    -    │      1       │   1    │   -    │
└─────────────────────┴─────────┴──────────┴─────────┴──────────────┴────────┴────────┘
```

With `--insecure`, an additional `Origin` column (rendered bold red) sits
between `Source` and `Prompts`. Values are `direct` for HTTP deps declared
in `apm.yml` and `via <parent>` for transitive HTTP deps pulled in by
another package:

```
┌─────────────────┬─────────┬──────────┬────────────────┬─────────┬──────────────┬────────┬────────┐
│ Package         │ Version │ Source   │ Origin         │ Prompts │ Instructions │ Agents │ Skills │
├─────────────────┼─────────┼──────────┼────────────────┼─────────┼──────────────┼────────┼────────┤
│ internal-pkg    │ 1.0.0   │ github   │ direct         │    1    │      -       │   -    │   -    │
│ shared-rules    │ 2.0.0   │ github   │ via acme/pkg   │    -    │      1       │   -    │   -    │
└─────────────────┴─────────┴──────────┴────────────────┴─────────┴──────────────┴────────┴────────┘
```

**Output includes:**
- Package name and version
- Source information
- Per-primitive counts (prompts, instructions, agents, skills)

#### `apm deps tree` - Show dependency tree structure

Display dependencies in hierarchical tree format with primitive counts.

```bash
apm deps tree  
```

**Examples:**
```bash
# Show dependency tree
apm deps tree
```

**Sample Output:**
```
company-website (local)
├── compliance-rules@1.0.0
│   ├── 1 instructions
│   ├── 1 chatmodes
│   └── 3 agent workflows
└── design-guidelines@1.0.0
    ├── 1 instructions
    └── 3 agent workflows
```

**Output format:**
- Hierarchical tree showing project name and dependencies
- File counts grouped by type (instructions, chatmodes, agent workflows)
- Version numbers from dependency package metadata
- Version information for each dependency

#### `apm deps info` - Alias for `apm view`

Backward-compatible alias for `apm view PACKAGE_NAME`.

```bash
apm deps info PACKAGE_NAME
```

**Arguments:**
- `PACKAGE_NAME` - Installed package name to inspect

**Examples:**
```bash
# Show installed package metadata
apm deps info compliance-rules
```

**Notes:**
- Produces the same local metadata output as `apm view PACKAGE_NAME`
- Use `apm view` in new docs and scripts
- For remote refs, use `apm view PACKAGE_NAME versions`

#### `apm deps clean` - Remove all APM dependencies

Remove the entire `apm_modules/` directory and all installed APM packages.

```bash
apm deps clean [OPTIONS]
```

**Options:**
- `--dry-run` - Show what would be removed without removing
- `--yes`, `-y` - Skip confirmation prompt (for non-interactive/scripted use)

**Examples:**
```bash
# Remove all APM dependencies (with confirmation)
apm deps clean

# Preview what would be removed
apm deps clean --dry-run

# Remove without confirmation (e.g. in CI pipelines)
apm deps clean --yes
```

**Behavior:**
- Shows confirmation prompt before deletion (unless `--yes` is provided)
- Removes entire `apm_modules/` directory
- Displays count of packages that will be removed
- Can be cancelled with Ctrl+C or 'n' response

#### `apm deps update` - Update APM dependencies

Re-resolve git references for all dependencies (direct and transitive) to their
latest commits, download updated content, re-integrate primitives, and regenerate
the lockfile.

```bash
apm deps update [PACKAGES...] [OPTIONS]
```

**Arguments:**
- `PACKAGES` - Optional. One or more packages to update. Omit to update all.

**Options:**
- `--verbose, -v` - Show detailed update information
- `--force` - Overwrite locally-authored files on collision
- `-g, --global` - Update user-scope dependencies (`~/.apm/`)
- `--target, -t` - Force deployment to specific target(s). Accepts comma-separated values (e.g., `-t claude,copilot`). Valid values: copilot, claude, cursor, opencode, gemini, vscode, agents, all
- `--parallel-downloads` - Max concurrent downloads (default: 4)

**Policy enforcement:** `apm deps update` runs the install pipeline and is therefore gated by org `apm-policy.yml`. There is no `--no-policy` flag on this command -- the only escape hatch is `APM_POLICY_DISABLE=1` for the shell session. See [Policy reference](../../enterprise/policy-reference/#install-time-enforcement).

**Examples:**
```bash
# Update all APM dependencies to latest refs
apm deps update

# Update a specific package (short name or full owner/repo)
apm deps update owner/compliance-rules

# Update multiple packages
apm deps update org/pkg-a org/pkg-b

# Update with verbose output
apm deps update --verbose

# Force overwrite local files on collision
apm deps update --force
```

### `apm mcp` - Browse MCP server registry

Browse and discover MCP servers from the GitHub MCP Registry.

```bash
apm mcp COMMAND [OPTIONS]
```

All `apm mcp` subcommands and `apm install --mcp` honour the [`MCP_REGISTRY_URL`](../../guides/mcp-servers/#custom-registry-enterprise) environment variable for custom (e.g. enterprise) MCP registries.

#### `apm mcp install` - Add an MCP server (alias)

Alias for [`apm install --mcp`](#apm-install---install-dependencies-and-deploy-local-content). Forwards every argument and flag. See the [MCP Servers guide](../../guides/mcp-servers/) for the full reference.

```bash
apm mcp install NAME [OPTIONS] [-- COMMAND ARGV...]
```

**Arguments:**
- `NAME` - MCP server name. Use a registry name for registry installs, or a local name for self-defined stdio and remote servers.

**Options:**
- `--transport [stdio|http|sse|streamable-http]` - MCP transport. Inferred from `--url` or post-`--` argv when omitted.
- `--url URL` - MCP server URL for `http`, `sse`, or `streamable-http` transports.
- `--env KEY=VALUE` - Environment variable for stdio MCP servers. Repeatable.
- `--header KEY=VALUE` - HTTP header for remote MCP servers. Repeatable.
- `--mcp-version VER` - Pin a registry MCP entry to a specific version.
- `--registry URL` - Custom MCP registry URL for resolving `NAME`.
- `--dev` - Add the server to `devDependencies`.
- `--dry-run` - Show what would be added without writing.
- `--force` - Replace an existing MCP entry.
- `-v, --verbose` - Show detailed output.
- `--no-policy` - Skip org policy enforcement for this invocation.

**Examples:**
```bash
# stdio (post-`--` argv)
apm mcp install filesystem -- npx -y @modelcontextprotocol/server-filesystem /workspace

# Registry
apm mcp install io.github.github/github-mcp-server

# Remote
apm mcp install linear --transport http --url https://mcp.linear.app/sse
```

Set the [`MCP_REGISTRY_URL`](../../guides/mcp-servers/#custom-registry-enterprise) environment variable to point all `apm mcp` commands and `apm install --mcp` at a custom MCP registry. The URL must use `https://`; set `MCP_REGISTRY_ALLOW_HTTP=1` to opt in to plaintext `http://` for development. When a custom registry is set and unreachable during install pre-flight, network errors are fatal (the default registry keeps the existing assume-valid behaviour).

#### `apm mcp list` - List MCP servers

List all available MCP servers from the registry.

```bash
apm mcp list [OPTIONS]
```

**Options:**
- `--limit INTEGER` - Number of results to show (default: 20)

**Examples:**
```bash
# List available MCP servers
apm mcp list

# Limit results
apm mcp list --limit 20
```

#### `apm mcp search` - Search MCP servers

Search for MCP servers in the GitHub MCP Registry.

```bash
apm mcp search QUERY [OPTIONS]
```

**Arguments:**
- `QUERY` - Search term to find MCP servers

**Options:**
- `--limit INTEGER` - Number of results to show (default: 10)

**Examples:**
```bash
# Search for filesystem-related servers
apm mcp search filesystem

# Search with custom limit
apm mcp search database --limit 5

# Search for GitHub integration
apm mcp search github
```

#### `apm mcp show` - Show MCP server details

Show detailed information about a specific MCP server from the registry.

```bash
apm mcp show SERVER_NAME
```

**Arguments:**
- `SERVER_NAME` - Name or ID of the MCP server to show

**Examples:**
```bash
# Show details for a server by name
apm mcp show @modelcontextprotocol/servers/src/filesystem

# Show details by server ID
apm mcp show a5e8a7f0-d4e4-4a1d-b12f-2896a23fd4f1
```

**Output includes:**
- Server name and description
- Latest version information
- Repository URL
- Available installation packages
- Installation instructions

### `apm marketplace` - Plugin marketplace management

Register, browse, and manage plugin marketplaces. Marketplaces are GitHub repositories containing a `marketplace.json` index of plugins.

> See the [Marketplaces guide](../../guides/marketplaces/) for concepts and workflows.

```bash
apm marketplace COMMAND [OPTIONS]
```

#### `apm marketplace add` - Register a marketplace

Register a GitHub repository as a plugin marketplace.

```bash
apm marketplace add OWNER/REPO [OPTIONS]
apm marketplace add HOST/OWNER/REPO [OPTIONS]
```

**Arguments:**
- `OWNER/REPO` - GitHub repository containing `marketplace.json`
- `HOST/OWNER/REPO` - Repository on a non-github.com host (e.g., GitHub Enterprise)

**Options:**
- `-n, --name TEXT` - Custom display name for the marketplace
- `-b, --branch TEXT` - Branch to track (default: main)
- `--host TEXT` - Git host FQDN (default: github.com or `GITHUB_HOST` env var)
- `-v, --verbose` - Show detailed output

**Examples:**
```bash
# Register a marketplace
apm marketplace add acme/plugin-marketplace

# Register with a custom name and branch
apm marketplace add acme/plugin-marketplace --name acme-plugins --branch release

# Register from a GitHub Enterprise host
apm marketplace add acme/plugin-marketplace --host ghes.corp.example.com
apm marketplace add ghes.corp.example.com/acme/plugin-marketplace
```

#### `apm marketplace list` - List registered marketplaces

List all registered marketplaces with their source repository and branch.

```bash
apm marketplace list [OPTIONS]
```

**Options:**
- `-v, --verbose` - Show detailed output

**Examples:**
```bash
apm marketplace list
```

#### `apm marketplace browse` - Browse marketplace plugins

List all plugins available in a registered marketplace.

```bash
apm marketplace browse NAME [OPTIONS]
```

**Arguments:**
- `NAME` - Name of the registered marketplace

**Options:**
- `-v, --verbose` - Show detailed output

**Examples:**
```bash
# Browse all plugins in a marketplace
apm marketplace browse acme-plugins
```

#### `apm marketplace update` - Refresh marketplace cache

Refresh the cached `marketplace.json` for one or all registered marketplaces.

```bash
apm marketplace update [NAME] [OPTIONS]
```

**Arguments:**
- `NAME` - Optional marketplace name. Omit to refresh all.

**Options:**
- `-v, --verbose` - Show detailed output

**Examples:**
```bash
# Refresh a specific marketplace
apm marketplace update acme-plugins

# Refresh all marketplaces
apm marketplace update
```

#### `apm marketplace remove` - Remove a registered marketplace

Unregister a marketplace. Plugins previously installed from it remain pinned in `apm.lock.yaml`.

```bash
apm marketplace remove NAME [OPTIONS]
```

**Arguments:**
- `NAME` - Name of the marketplace to remove

**Options:**
- `-y, --yes` - Skip confirmation prompt
- `-v, --verbose` - Show detailed output

**Examples:**
```bash
# Remove with confirmation prompt
apm marketplace remove acme-plugins

# Remove without confirmation
apm marketplace remove acme-plugins --yes
```

#### `apm marketplace validate` - Validate a marketplace manifest

Validate `marketplace.json` for schema errors and duplicate plugin names.

```bash
apm marketplace validate NAME [OPTIONS]
```

**Arguments:**
- `NAME` - Name of the marketplace to validate

**Options:**
- `--check-refs` - Verify version refs are reachable (network). *Not yet implemented.*
- `-v, --verbose` - Show detailed output

**Examples:**
```bash
# Validate a marketplace
apm marketplace validate acme-plugins

# Verbose output
apm marketplace validate acme-plugins --verbose
```

#### `apm marketplace init` - Add a marketplace block to apm.yml

Add a `marketplace:` block to the project's `apm.yml`. If `apm.yml` is absent, a minimal one is scaffolded first. The block is richly commented and ready to be edited. Build the marketplace with [`apm pack`](#apm-pack---pack-distributable-artifacts). See the [Authoring a marketplace guide](../../guides/marketplace-authoring/).

```bash
apm marketplace init [OPTIONS]
```

**Options:**
- `--force` - Overwrite an existing `marketplace:` block in `apm.yml`
- `--no-gitignore-check` - Skip the `.gitignore` staleness check
- `--name TEXT` - Marketplace/package name (defaults to `my-marketplace` when scaffolding apm.yml)
- `--owner TEXT` - Owner name for the marketplace block
- `-v, --verbose` - Show detailed output

**Exit codes:**
- `0` - Block written
- `1` - Block already exists (without `--force`) or write failure

**Examples:**
```bash
apm marketplace init
apm marketplace init --force --owner acme-org
```

`apm init --marketplace` is the equivalent shortcut at project-creation time: it seeds a fresh `apm.yml` with the `marketplace:` block already in place.

#### `apm marketplace migrate` - Fold marketplace.yml into apm.yml

One-shot conversion of a legacy standalone `marketplace.yml` into the `marketplace:` block of `apm.yml`. Inheritable fields (`name`, `description`, `version`) are dropped from the block when they match `apm.yml`'s top-level values, and emitted as overrides when they differ. The legacy `marketplace.yml` is deleted on success.

```bash
apm marketplace migrate [OPTIONS]
```

**Options:**
- `--force`, `--yes`, `-y` - Overwrite an existing `marketplace:` block in `apm.yml` (the three flags are aliases)
- `--dry-run` - Print the proposed change without writing
- `-v, --verbose` - Show detailed output

**Exit codes:**
- `0` - Migration applied (or dry run complete)
- `1` - Migration failed (legacy file missing, conflict without `--force`, write failure)

**Examples:**
```bash
apm marketplace migrate --dry-run
apm marketplace migrate --yes
```

#### `apm marketplace outdated` - Report available upgrades

List packages in the `marketplace:` block whose source repositories have newer tags available. Range-aware: distinguishes "latest in range" (picked up by next `build`) from "latest overall" (requires a manual range bump). Local-path packages and `ref:`-pinned entries show `--` in the range columns.

```bash
apm marketplace outdated [OPTIONS]
```

**Options:**
- `--offline` - Use cached refs only
- `--include-prerelease` - Include pre-release tags
- `-v, --verbose` - Show detailed output

**Exit codes:**
- `0` - Report rendered (even if upgrades are available)
- `1` - Unable to query refs
- `2` - Schema error in the `marketplace:` block

**Examples:**
```bash
apm marketplace outdated
apm marketplace outdated --include-prerelease
```

#### `apm marketplace check` - Validate marketplace entries

Validate the `marketplace:` schema and verify that every package entry is resolvable (ref exists, at least one tag satisfies the range). Intended for CI use before publishing.

```bash
apm marketplace check [OPTIONS]
```

**Options:**
- `--offline` - Schema and cached-ref checks only (no network)
- `-v, --verbose` - Show detailed output

**Exit codes:**
- `0` - All entries OK
- `1` - One or more entries are unreachable or unresolvable
- `2` - Schema error in the `marketplace:` block

**Examples:**
```bash
apm marketplace check
apm marketplace check --offline
```

#### `apm marketplace doctor` - Environment diagnostics

Check git, network reachability, authentication, `gh` CLI availability, and the presence of a marketplace config (in `apm.yml` or legacy `marketplace.yml`). Run this first when `apm pack` or `publish` fails in an unfamiliar environment.

```bash
apm marketplace doctor [OPTIONS]
```

**Options:**
- `-v, --verbose` - Per-check detail

**Exit codes:**
- `0` - All checks pass
- `1` - One or more checks failed

**Examples:**
```bash
apm marketplace doctor
apm marketplace doctor --verbose
```

#### `apm marketplace publish` - Open PRs on consumer repositories

Drive the compiled `marketplace.json` out to consumer repositories listed in a `consumer-targets.yml` file, opening a pull request on each. Requires an authenticated `gh` CLI unless `--no-pr` is used. Run `apm pack` first to (re)build `marketplace.json`. See the [Authoring a marketplace guide](../../guides/marketplace-authoring/#publishing-to-consumers) for the full workflow.

```bash
apm marketplace publish [OPTIONS]
```

**Options:**
- `--targets PATH` - Path to the targets file (default: `./consumer-targets.yml`)
- `--dry-run` - Preview without pushing or opening PRs
- `--no-pr` - Push branches but skip PR creation
- `--draft` - Create PRs as drafts
- `--allow-downgrade` - Allow pushing a lower version than the target currently references
- `--allow-ref-change` - Allow switching ref types (for example, branch to SHA)
- `--parallel N` - Maximum concurrent target updates (default: `4`)
- `-y, --yes` - Skip the confirmation prompt (required in non-interactive sessions)
- `-v, --verbose` - Per-target detail

**Exit codes:**
- `0` - All targets succeeded (or were already up to date)
- `1` - One or more targets failed, or prerequisites missing

**Examples:**
```bash
# Preview the publish plan
apm marketplace publish --dry-run --yes

# Publish with PRs
apm marketplace publish

# Push branches only (no gh CLI needed)
apm marketplace publish --no-pr
```

Run history and PR URLs are recorded in `.apm/publish-state.json` so re-runs can detect existing PRs.

#### `apm marketplace package add` - Add a package entry

Add a package entry to the `marketplace.packages` list in `apm.yml`.

```bash
apm marketplace package add SOURCE [OPTIONS]
```

**Arguments:**
- `SOURCE` - GitHub `owner/repo` reference

**Options:**
- `--version TEXT` - Semver range constraint (e.g. `">=1.0.0"`)
- `--ref TEXT` - Pin to a git ref (SHA, tag, or HEAD). Mutable refs are auto-resolved to SHA
- `-d`, `--description TEXT` - Short description for the entry
- `-s`, `--subdir TEXT` - Subdirectory inside source repo
- `--include-prerelease` - Include pre-release versions
- `--no-verify` - Skip remote repository verification
- `--verbose` - Enable verbose output

`--version` and `--ref` are mutually exclusive. When neither is provided, the current `HEAD` SHA is pinned automatically.

**Examples:**
```bash
# Add a package with a version range
apm marketplace package add acme/code-review --version ">=1.0.0"

# Pin to a specific tag
apm marketplace package add acme/code-review --ref v2.1.0

# Pin to current HEAD (auto-resolved to SHA)
apm marketplace package add acme/code-review

# Add with description and skip verification (requires explicit --ref SHA)
apm marketplace package add acme/code-review --ref abc123...40chars \
  --description "Code review skill" --no-verify
```

#### `apm marketplace package set` - Update a package entry

Update fields on an existing package entry in the `marketplace.packages` list of `apm.yml`.

```bash
apm marketplace package set NAME [OPTIONS]
```

**Arguments:**
- `NAME` - Name of the existing package entry

**Options:**
- `--version TEXT` - New semver range constraint
- `--ref TEXT` - New git ref (SHA, tag, or HEAD). Mutable refs are auto-resolved to SHA
- `--description TEXT` - New description
- `--include-prerelease` - Enable pre-release version inclusion
- `--verbose` - Enable verbose output

`--version` and `--ref` are mutually exclusive. At least one field option must be specified.

**Examples:**
```bash
# Widen the version range
apm marketplace package set code-review --version ">=2.0.0"

# Switch from version to pinned ref
apm marketplace package set code-review --ref abc1234

# Re-pin to current HEAD SHA
apm marketplace package set code-review --ref HEAD

# Update the description
apm marketplace package set code-review --description "Updated review skill"
```

#### `apm marketplace package remove` - Remove a package entry

Remove a package entry from the `marketplace.packages` list in `apm.yml`.

```bash
apm marketplace package remove NAME [OPTIONS]
```

**Arguments:**
- `NAME` - Name of the package entry to remove

**Options:**
- `--yes` - Skip confirmation prompt
- `--verbose` - Enable verbose output

Prompts for confirmation unless `--yes` is passed. In non-interactive environments (CI), use `--yes`.

**Examples:**
```bash
# Remove with confirmation prompt
apm marketplace package remove code-review

# Skip confirmation (CI-friendly)
apm marketplace package remove code-review --yes
```

### `apm search` - Search plugins in a marketplace

Search for plugins by name or description within a specific marketplace.

```bash
apm search QUERY@MARKETPLACE [OPTIONS]
```

**Arguments:**
- `QUERY@MARKETPLACE` - Search term scoped to a marketplace (e.g., `security@skills`)

**Options:**
- `--limit INTEGER` - Maximum results to return (default: 20)
- `-v, --verbose` - Show detailed output

**Examples:**
```bash
# Search for code review plugins in a marketplace
apm search "code review@skills"

# Limit results
apm search "linting@awesome-copilot" --limit 5
```

### `apm run` (Experimental) - Execute prompts

Execute a script defined in your apm.yml with parameters and real-time output streaming.

> See the [Agent Workflows guide](../../guides/agent-workflows/) for usage details.

```bash
apm run [SCRIPT_NAME] [OPTIONS]
```

**Arguments:**
- `SCRIPT_NAME` - Name of script to run from apm.yml scripts section

**Options:**
- `-p, --param TEXT` - Parameter in format `name=value` (can be used multiple times)
- `-v, --verbose` - Show detailed output

**Examples:**
```bash
# Run start script (default script)
apm run start --param name="<YourGitHubHandle>"

# Run with different scripts 
apm run start --param name="Alice"
apm run llm --param service=api
apm run debug --param service=api

# Run specific scripts with parameters
apm run llm --param service=api --param environment=prod
```

**Return Codes:**
- `0` - Success
- `1` - Execution failed or error occurred

### `apm preview` - Preview compiled scripts

Show the processed prompt content with parameters substituted, without executing.

```bash
apm preview [SCRIPT_NAME] [OPTIONS]
```

**Arguments:**
- `SCRIPT_NAME` - Name of script to preview from apm.yml scripts section

**Options:**
- `-p, --param TEXT` - Parameter in format `name=value`
- `-v, --verbose` - Show detailed output

**Examples:**
```bash
# Preview start script
apm preview start --param name="<YourGitHubHandle>"

# Preview specific script with parameters
apm preview llm --param name="Alice"
```

### `apm list` - List available scripts

Display all scripts defined in apm.yml.

```bash
apm list
```

**Examples:**
```bash
# List all prompts in project
apm list
```

**Output format:**
```
Available scripts:
  start: codex hello-world.prompt.md
  llm: llm hello-world.prompt.md -m github/gpt-4o-mini  
  debug: RUST_LOG=debug codex hello-world.prompt.md
```

### `apm compile` - Compile APM context into distributed AGENTS.md files

Compile APM context files (chatmodes, instructions, contexts) into distributed AGENTS.md files with conditional sections, markdown link resolution, and project setup auto-detection.

```bash
apm compile [OPTIONS]
```

**Options:**
- `-o, --output TEXT` - Output file path (for single-file mode)
- `-t, --target [vscode|agents|claude|codex|opencode|gemini|all]` - Target agent format. Accepts comma-separated values for multiple targets (e.g., `-t claude,copilot`). `agents` is an alias for `vscode`. Auto-detects if not specified.
- `--chatmode TEXT` - Chatmode to prepend to the AGENTS.md file
- `--dry-run` - Preview compilation without writing files (shows placement decisions)
- `--no-links` - Skip markdown link resolution
- `--with-constitution/--no-constitution` - Include Spec Kit `memory/constitution.md` verbatim at top inside a delimited block (default: `--with-constitution`). When disabled, any existing block is preserved but not regenerated.
- `--watch` - Auto-regenerate on changes (file system monitoring)
- `--validate` - Validate primitives without compiling
- `--single-agents` - Force single-file compilation (legacy mode)
- `-v, --verbose` - Show detailed source attribution and optimizer analysis
- `--local-only` - Ignore dependencies, compile only local primitives
- `--clean` - Remove orphaned AGENTS.md files that are no longer generated

**Target Auto-Detection:**

When `--target` is not specified, APM auto-detects based on existing project structure:

| Condition | Target | Output |
|-----------|--------|--------|
| `.github/` exists only | `vscode` | AGENTS.md + .github/ |
| `.claude/` exists only | `claude` | CLAUDE.md + .claude/ |
| `.codex/` exists | `codex` | AGENTS.md + .codex/ + .agents/ |
| `.gemini/` exists | `gemini` | GEMINI.md + .gemini/ |
| Both folders exist | `all` | All outputs |
| Neither folder exists | `minimal` | AGENTS.md only |

You can also set a persistent target in `apm.yml`:
```yaml
name: my-project
version: 1.0.0
target: vscode  # single target
```

```yaml
name: my-project
version: 1.0.0
target: [claude, copilot]  # multiple targets -- only these are compiled/installed
```

**Target Formats (explicit):**

| Target | Output Files | Best For |
|--------|--------------|----------|
| `vscode` | AGENTS.md, .github/prompts/, .github/agents/, .github/skills/ | GitHub Copilot, Cursor |
| `claude` | CLAUDE.md, .claude/commands/, SKILL.md | Claude Code, Claude Desktop |
| `codex` | AGENTS.md, .agents/skills/, .codex/agents/, .codex/hooks.json | Codex CLI |
| `opencode` | AGENTS.md, .opencode/agents/, .opencode/commands/, .opencode/skills/ | OpenCode |
| `gemini` | GEMINI.md, .gemini/commands/, .gemini/skills/ | Gemini CLI |
| `all` | All of the above | Universal compatibility |

**Examples:**
```bash
# Basic compilation with auto-detected context
apm compile

# Generate with specific chatmode
apm compile --chatmode architect

# Preview without writing file
apm compile --dry-run

# Custom output file
apm compile --output docs/AI-CONTEXT.md

# Validate context without generating output
apm compile --validate

# Watch for changes and auto-recompile (development mode)
apm compile --watch

# Watch mode with dry-run for testing
apm compile --watch --dry-run

# Target specific agent formats
apm compile --target vscode    # AGENTS.md + .github/ only
apm compile --target claude    # CLAUDE.md + .claude/ only
apm compile --target opencode  # AGENTS.md + .opencode/ only
apm compile --target all       # All formats (default)

# Multiple targets (comma-separated)
apm compile -t claude,copilot  # Both CLAUDE.md and AGENTS.md

# Compile injecting Spec Kit constitution (auto-detected)
apm compile --with-constitution

# Recompile WITHOUT updating the block but preserving previous injection
apm compile --no-constitution
```

**Watch Mode:**
- Monitors `.apm/`, `.github/instructions/`, `.github/chatmodes/` directories
- Auto-recompiles when `.md` or `apm.yml` files change
- Includes 1-second debounce to prevent rapid recompilation
- Press Ctrl+C to stop watching
- Requires `watchdog` library (automatically installed)

**Validation Mode:**
- Checks primitive structure and frontmatter completeness
- Displays actionable suggestions for fixing validation errors
- Exits with error code 1 if validation fails
- No output file generation in validation-only mode

**Content Scanning:**
Compiled output is scanned for hidden Unicode characters before writing to disk. Critical findings cause `apm compile` to exit with code 1 — defense-in-depth since source files are already scanned during `apm install`.

**Configuration Integration:**
The compile command supports configuration via `apm.yml`:

```yaml
compilation:
  output: "AGENTS.md"           # Default output file
  chatmode: "backend-engineer"  # Default chatmode to use
  resolve_links: true           # Enable markdown link resolution
  exclude:                      # Directory exclusion patterns (glob syntax)
    - "apm_modules/**"          # Exclude installed packages
    - "tmp/**"                  # Exclude temporary files
    - "coverage/**"             # Exclude test coverage
    - "**/test-fixtures/**"     # Exclude test fixtures at any depth
```

**Directory Exclusion Patterns:**

Use the `exclude` field to skip directories during compilation. This improves performance in large monorepos and prevents duplicate instruction discovery from source package development directories.

**Pattern examples:**
- `tmp` - Matches directory named "tmp" at any depth
- `projects/packages/apm` - Matches specific nested path
- `**/node_modules` - Matches "node_modules" at any depth
- `coverage/**` - Matches "coverage" and all subdirectories
- `projects/**/apm/**` - Complex nested matching with `**`

**Default exclusions** (always applied, matched on exact path components):
- `node_modules`, `__pycache__`, `.git`, `dist`, `build`, `apm_modules`
- Hidden directories (starting with `.`)

Command-line options always override `apm.yml` settings. Priority order:
1. Command-line flags (highest priority)
2. `apm.yml` compilation section
3. Built-in defaults (lowest priority)

**Generated AGENTS.md structure:**
- **Header** - Generation metadata and APM version
- **(Optional) Spec Kit Constitution Block** - Delimited block:
  - Markers: `<!-- SPEC-KIT CONSTITUTION: BEGIN -->` / `<!-- SPEC-KIT CONSTITUTION: END -->`
  - Second line includes `hash: <sha256_12>` for drift detection
  - Entire raw file content in between (Phase 0: no summarization)
- **Pattern-based Sections** - Content grouped by exact `applyTo` patterns from instruction context files (e.g., "Files matching `**/*.py`")
- **Footer** - Regeneration instructions

The structure is entirely dictated by the instruction context found in `.apm/` and `.github/instructions/` directories. No predefined sections or project detection are applied.

**Primitive Discovery:**
- **Chatmodes**: `.chatmode.md` files in `.apm/chatmodes/`, `.github/chatmodes/`
- **Instructions**: `.instructions.md` files in `.apm/instructions/`, `.github/instructions/`
- **Workflows**: `.prompt.md` files in project and `.github/prompts/`

APM integrates seamlessly with [Spec-kit](https://github.com/github/spec-kit) for specification-driven development, automatically injecting Spec-kit `constitution` into the compiled context layer.

### `apm config` - Configure APM CLI

Manage APM CLI configuration settings. Running `apm config` without subcommands displays the current configuration.

```bash
apm config [COMMAND]
```

#### `apm config` - Show current configuration (default behavior)

Display current APM CLI configuration and project settings.

```bash
apm config
```

**What's displayed:**
- Project configuration from `apm.yml` (if in an APM project)
  - Project name, version, entrypoint
  - Number of MCP dependencies
  - Compilation settings (output, chatmode, resolve_links)
- Global configuration
  - APM CLI version
  - `auto-integrate` setting
  - `temp-dir` setting (when configured)

**Examples:**
```bash
# Show current configuration
apm config
```

#### `apm config get` - Get a configuration value

Get a specific configuration value or display all configuration values.

```bash
apm config get [KEY]
```

**Arguments:**
- `KEY` (optional) - Configuration key to retrieve. Supported keys:
  - `auto-integrate` - Whether to automatically integrate `.prompt.md` files into AGENTS.md
  - `temp-dir` - Custom temporary directory for clone/download operations
  - `copilot-cowork-skills-dir` - Override the resolved Cowork OneDrive skills directory

If `KEY` is omitted, displays all configuration values.

**Examples:**
```bash
# Get auto-integrate setting
apm config get auto-integrate

# Show all configuration
apm config get
```

#### `apm config set` - Set a configuration value

Set a configuration value globally for APM CLI.

```bash
apm config set KEY VALUE
```

**Arguments:**
- `KEY` - Configuration key to set. Supported keys:
  - `auto-integrate` - Enable/disable automatic integration of `.prompt.md` files
  - `temp-dir` - Set a custom temporary directory path
  - `copilot-cowork-skills-dir` - Override the resolved Cowork OneDrive skills directory
- `VALUE` - Value to set. For boolean keys, use: `true`, `false`, `yes`, `no`, `1`, `0`

**Configuration Keys:**

**`auto-integrate`** - Control automatic prompt integration
- **Type:** Boolean
- **Default:** `true`
- **Description:** When enabled, APM automatically discovers and integrates `.prompt.md` files from `.github/prompts/` and `.apm/prompts/` directories into the compiled AGENTS.md file. This ensures all prompts are available to coding agents without manual compilation.
- **Use Cases:**
  - Set to `false` if you want to manually manage which prompts are compiled
  - Set to `true` to ensure all prompts are always included in the context

**Examples:**
```bash
# Enable auto-integration (default)
apm config set auto-integrate true

# Disable auto-integration
apm config set auto-integrate false
```

**`temp-dir`** - Override the system temporary directory
- **Type:** String (directory path)
- **Default:** System temp directory (not stored)
- **Description:** Set a custom temporary directory for clone and download operations. Useful in corporate Windows environments where endpoint security software restricts access to `%TEMP%`, causing `[WinError 5] Access is denied`.
- **Resolution order:** `APM_TEMP_DIR` environment variable > `temp_dir` in `~/.apm/config.json` > system default.
- **Use Cases:**
  - Set when the default system temp directory is restricted or unavailable
  - Use the `APM_TEMP_DIR` environment variable for CI pipelines or per-session overrides

**Examples:**
```bash
# Set a custom temp directory (Windows)
apm config set temp-dir C:\apm-temp

# Set a custom temp directory (macOS/Linux)
apm config set temp-dir /tmp/apm-work

# Check the current temp-dir setting
apm config get temp-dir

# Or use the environment variable instead
export APM_TEMP_DIR=/tmp/apm-work
```

**`copilot-cowork-skills-dir`** - Override the resolved Cowork OneDrive skills directory
- **Type:** String (absolute directory path)
- **Default:** Auto-detected Cowork skills directory (not stored)
- **Description:** Override the resolved Cowork OneDrive skills directory. Gated on the `copilot-cowork` experimental flag for `set`; `get` and `unset` are always available for cleanup.
- **Resolution order:** `APM_COPILOT_COWORK_SKILLS_DIR` environment variable > `copilot_cowork_skills_dir` in `~/.apm/config.json` > platform auto-detection.
- **Use Cases:**
  - Set a specific OneDrive-backed Cowork skills directory instead of relying on auto-detection
  - Clear the override with `apm config unset copilot-cowork-skills-dir` when returning to auto-detection

**Examples:**
```bash
# Enable the experimental flag, then set an explicit Cowork skills directory
apm experimental enable copilot-cowork
apm config set copilot-cowork-skills-dir ~/Library/CloudStorage/OneDrive-Contoso/Documents/Cowork/skills

# Check the current copilot-cowork-skills-dir setting
apm config get copilot-cowork-skills-dir

# Remove the override and return to auto-detection
apm config unset copilot-cowork-skills-dir
```

See also: [Cowork integration](../integrations/copilot-cowork/).

## Runtime Management (Experimental)

### `apm runtime` (Experimental) - Manage AI runtimes

APM manages AI runtime installation and configuration automatically. Currently supports four runtimes: `copilot`, `codex`, `llm`, and `gemini`.

> See the [Agent Workflows guide](../../guides/agent-workflows/) for usage details.

```bash
apm runtime COMMAND [OPTIONS]
```

**Supported Runtimes:**
- **`copilot`** - GitHub Copilot coding agent
- **`codex`** - OpenAI Codex CLI with GitHub Models support
- **`llm`** - Simon Willison's LLM library with multiple providers
- **`gemini`** - Google Gemini CLI

#### `apm runtime setup` - Install AI runtime

Download and configure an AI runtime from official sources.

```bash
apm runtime setup [OPTIONS] {copilot|codex|llm|gemini}
```

**Arguments:**
- `{copilot|codex|llm|gemini}` - Runtime to install

**Options:**
- `--version TEXT` - Specific version to install
- `--vanilla` - Install runtime without APM configuration (uses runtime's native defaults)

**Examples:**
```bash
# Install Codex with APM defaults
apm runtime setup codex

# Install LLM with APM defaults  
apm runtime setup llm
```

**Windows support:**
- On Windows, APM runs the setup scripts through PowerShell automatically
- No special flags are required
- Platform detection is automatic

**Default Behavior:**
- Installs runtime binary from official sources
- Configures with GitHub Models (free) as APM default
- Creates Codex runtime configuration (global `~/.codex/config.toml`; project MCP config is managed separately in `.codex/config.toml`)
- Provides clear logging about what's being configured

**Vanilla Behavior (`--vanilla` flag):**
- Installs runtime binary only
- No APM-specific configuration applied
- Uses runtime's native defaults (e.g., OpenAI for Codex)
- No configuration files created by APM

#### `apm runtime list` - Show installed runtimes

List all available runtimes and their installation status.

```bash
apm runtime list
```

**Output includes:**
- Runtime name and description
- Installation status ([+] Installed / [x] Not installed)
- Installation path and version
- Configuration details

#### `apm runtime remove` - Uninstall runtime

Remove an installed runtime and its configuration.

```bash
apm runtime remove [OPTIONS] {copilot|codex|llm|gemini}
```

**Arguments:**
- `{copilot|codex|llm|gemini}` - Runtime to remove

**Options:**
- `-y, --yes` - Confirm the action without prompting

#### `apm runtime status` - Show active runtime and preference order

Display which runtime APM will use for execution and runtime preference order.

```bash
apm runtime status
```

**Output includes:**
- Runtime preference order (copilot → codex → gemini → llm)
- Currently active runtime
- Next steps if no runtime is available

## Experimental Features

### `apm experimental` - Manage experimental feature flags

Manage opt-in flags that gate new or changing behaviour. Running `apm experimental` with no subcommand lists the available flags.

```bash
apm experimental [OPTIONS] COMMAND [ARGS]...
```

**Options:**
- `-v, --verbose` - Show verbose output

**Subcommands:**

| Command | Description |
|---------|-------------|
| `list` | List all experimental features |
| `enable NAME` | Enable an experimental feature |
| `disable NAME` | Disable an experimental feature |
| `reset [NAME]` | Reset one feature, or all features, to defaults |

#### `apm experimental list`

```bash
apm experimental list [OPTIONS]
```

**Options:**
- `--enabled` - Show only enabled features
- `--disabled` - Show only disabled features
- `--json` - Output as a JSON array
- `-v, --verbose` - Show detailed output

#### `apm experimental enable`

```bash
apm experimental enable NAME [OPTIONS]
```

**Arguments:**
- `NAME` - Experimental feature name

**Options:**
- `-v, --verbose` - Show verbose output

#### `apm experimental disable`

```bash
apm experimental disable NAME [OPTIONS]
```

**Arguments:**
- `NAME` - Experimental feature name

**Options:**
- `-v, --verbose` - Show verbose output

#### `apm experimental reset`

```bash
apm experimental reset [NAME] [OPTIONS]
```

**Arguments:**
- `NAME` - Optional experimental feature name. Omit to reset all feature overrides.

**Options:**
- `-y, --yes` - Skip the confirmation prompt when resetting all features
- `-v, --verbose` - Show verbose output

See the full reference in [Experimental Flags](../experimental/).
