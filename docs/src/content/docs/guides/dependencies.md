---
title: "Dependencies"
sidebar:
  order: 5
---

Complete guide to APM package dependency management - share and reuse context collections across projects for consistent, scalable AI-native development.

## What Are APM Dependencies?

APM dependencies are git repositories containing `.apm/` directories with context collections (instructions, chatmodes, contexts) and agent workflows (prompts). They enable teams to:

- **Share proven workflows** across projects and team members
- **Standardize compliance and design patterns** organization-wide
- **Build on tested context** instead of starting from scratch
- **Maintain consistency** across multiple repositories and teams

APM supports any git-accessible host — GitHub, GitLab, Bitbucket, self-hosted instances, and more.

## Dependency Types

APM supports multiple dependency types:

| Type | Detection | Example |
|------|-----------|---------|
| **APM Package** | Has `apm.yml` | `microsoft/apm-sample-package` |
| **Marketplace Plugin** | Has `plugin.json` (no `apm.yml`) | `github/awesome-copilot/plugins/context-engineering` |
| **Claude Skill** | Has `SKILL.md` (no `apm.yml`) | `ComposioHQ/awesome-claude-skills/brand-guidelines` |
| **Hook Package** | Has `hooks/*.json` (no `apm.yml` or `SKILL.md`) | `anthropics/claude-plugins-official/plugins/hookify` |
| **Virtual Subdirectory Package** | Folder path in monorepo | `ComposioHQ/awesome-claude-skills/mcp-builder` |
| **Virtual Subdirectory Package** | Folder path in repo | `github/awesome-copilot/skills/review-and-refactor` |
| **Local Path Package** | Path starts with `./`, `../`, or `/` | `./packages/my-shared-skills` |
| **ADO Package** | Azure DevOps repo | `dev.azure.com/org/project/_git/repo` or `dev.azure.com/org/My%20Project/_git/My%20Repo` |

**Virtual Subdirectory Packages** are skill folders from monorepos - they download an entire folder and may contain a SKILL.md plus resources.

**Virtual File Packages** download a single file (like a prompt or instruction) and integrate it directly.

### Claude Skills

Claude Skills are packages with a `SKILL.md` file that describe capabilities for AI agents. APM can install them and transform them for your target platform:

```bash
# Install a Claude Skill
apm install ComposioHQ/awesome-claude-skills/brand-guidelines

# For copilot target: generates .github/agents/brand-guidelines.agent.md
# For Claude target: keeps native SKILL.md format
```

#### Skill Integration During Install

Skills are integrated to `.github/skills/`:

| Source | Result |
|--------|--------|
| Package with `SKILL.md` | Skill folder copied to `.github/skills/{folder-name}/` |
| Package without `SKILL.md` | No skill folder created |

#### Skill Folder Naming

Skill folders use the **source folder name directly** (not flattened paths):

```
.github/skills/
├── brand-guidelines/      # From ComposioHQ/awesome-claude-skills/brand-guidelines
├── mcp-builder/           # From ComposioHQ/awesome-claude-skills/mcp-builder
└── apm-sample-package/     # From microsoft/apm-sample-package
```

→ See [Skills Guide](../skills/) for complete documentation.

## Quick Start

### 1. Add Dependencies to Your Project

Add APM dependencies to your `apm.yml` file:

```yaml
name: my-project
version: 1.0.0
dependencies:
  apm:
    # GitHub shorthand (default)
    - microsoft/apm-sample-package#v1.0.0
    - github/awesome-copilot/skills/review-and-refactor

    # Full HTTPS git URL (any host)
    - https://gitlab.com/acme/coding-standards.git
    - https://bitbucket.org/acme/security-rules.git

    # SSH git URL (any host)
    - git@gitlab.com:acme/coding-standards.git

    # FQDN shorthand with virtual path (any host)
    - gitlab.com/acme/repo/prompts/code-review.prompt.md

    # Local path (for development / monorepo workflows)
    - ./packages/my-shared-skills          # relative to project root
    - /home/user/repos/my-ai-package       # absolute path

    # Object format: git URL + sub-path / ref / alias
    - git: https://gitlab.com/acme/coding-standards.git
      path: instructions/security
      ref: v2.0
  mcp:
    - io.github.github/github-mcp-server          # Registry reference (string)
    - name: io.github.github/github-mcp-server      # Registry with overlays
      transport: stdio
      tools: ["repos", "issues"]
    - name: internal-knowledge-base                  # Self-defined (private server)
      registry: false
      transport: http
      url: "${KNOWLEDGE_BASE_URL}"
      env:
        KB_TOKEN: "${KB_TOKEN}"
```

APM accepts dependencies in two forms:

**String format** (simple cases):
- **Shorthand** (`owner/repo`) — defaults to GitHub
- **HTTPS URL** (`https://host/owner/repo.git`) — any git host, whole repo
  - Custom port: `https://host:8443/owner/repo.git` — port is preserved in clone URLs
- **SSH URL** (`git@host:owner/repo.git`) — any git host, whole repo
  - Custom port: `ssh://git@host:7999/owner/repo.git` — use the `ssh://` form to specify a port (SCP shorthand `git@host:...` cannot carry a port)
- **FQDN shorthand** (`host/owner/repo`) — any host, supports nested groups
  - GitLab nested groups: `gitlab.com/group/subgroup/repo`
  - Virtual paths on simple repos: `gitlab.com/owner/repo/file.prompt.md`
  - For nested groups + virtual paths, use the object format below
- **Local path** (`./path`, `../path`, `/absolute/path`) — local filesystem package

**Object format** (when you need `path`, `ref`, or `alias` on a git URL):

```yaml
dependencies:
  apm:
    - git: https://gitlab.com/acme/coding-standards.git
      path: instructions/security        # virtual sub-path inside the repo
      ref: v2.0                          # pin to a tag, branch, or commit
    - git: git@bitbucket.org:team/rules.git
      path: prompts/review.prompt.md
      alias: review                      # local alias (controls install directory name)
    - git: ssh://git@bitbucket.example.com:7999/project/repo.git  # Bitbucket Datacenter (custom SSH port)
      ref: v1.0
```

Fields: `git` (required), `path`, `ref`, `alias` (all optional). The `git` value is any HTTPS or SSH clone URL. Explicit URL schemes are honored exactly -- see [Transport selection](#transport-selection-ssh-vs-https) for the full contract. Custom ports are preserved across every attempt (including any cross-protocol fallback enabled with `--allow-protocol-fallback`), so `ssh://host:7999/...` retried over HTTPS becomes `https://host:7999/...`.

> **Nested groups (GitLab, Gitea, etc.):** APM treats all path segments after the host as the repo path, so `gitlab.com/group/subgroup/repo` resolves to a repo at `group/subgroup/repo`. Virtual paths on simple 2-segment repos work with shorthand (`gitlab.com/owner/repo/file.prompt.md`). But for **nested-group repos + virtual paths**, use the object format — the shorthand is ambiguous:
>
> ```yaml
> # DON'T — ambiguous: APM can't tell where the repo path ends
> # gitlab.com/group/subgroup/repo/file.prompt.md
> #   → parsed as repo=group/subgroup, virtual=repo/file.prompt.md (wrong!)
>
> # DO — explicit and unambiguous
> - git: gitlab.com/group/subgroup/repo
>   path: file.prompt.md
> ```

### How Dependencies Are Stored (Canonical Format)

APM normalizes every dependency entry on write — no matter how you specify a package, the stored form in `apm.yml` is always a clean, canonical string. This works like Docker's default registry convention:

- **GitHub** is the default registry. The `github.com` host is stripped, leaving just `owner/repo`.
- **Non-default hosts** (GitLab, Bitbucket, self-hosted) keep their FQDN: `gitlab.com/owner/repo`.

| You type | Stored in apm.yml |
|----------|-------------------|
| `microsoft/apm-sample-package` | `microsoft/apm-sample-package` |
| `https://github.com/microsoft/apm-sample-package.git` | `microsoft/apm-sample-package` |
| `git@github.com:microsoft/apm-sample-package.git` | `microsoft/apm-sample-package` |
| `github.com/microsoft/apm-sample-package` | `microsoft/apm-sample-package` |
| `https://gitlab.com/acme/rules.git` | `gitlab.com/acme/rules` |
| `gitlab.com/group/subgroup/repo` | `gitlab.com/group/subgroup/repo` |
| `git@gitlab.com:group/subgroup/repo.git` | `gitlab.com/group/subgroup/repo` |
| `git@bitbucket.org:team/standards.git` | `bitbucket.org/team/standards` |
| `./packages/my-skills` | `./packages/my-skills` |
| `/home/user/repos/my-pkg` | `/home/user/repos/my-pkg` |

Virtual paths and refs are preserved:

| You type | Stored in apm.yml |
|----------|-------------------|
| `github.com/org/repo/skills/review#v2` | `org/repo/skills/review#v2` |
| `https://gitlab.com/acme/repo.git` + path `docs` + ref `main` | `gitlab.com/acme/repo/docs#main` |

This normalization means:
- **Duplicate detection works** across input forms — you can't accidentally install the same package twice using different URL formats.
- **`apm uninstall` accepts any form** — shorthand, HTTPS URL, or SSH URL all resolve to the same canonical identity.
- **`apm.yml` stays clean** and readable regardless of how packages were added.

MCP dependencies resolve via the MCP server registry (e.g. `io.github.github/github-mcp-server`).

MCP dependencies declared by transitive APM packages are collected automatically during `apm install`.

### 2. Install Dependencies

```bash
# Install all dependencies
apm install

# Install only APM dependencies (faster)
apm install --only=apm

# Preview what will be installed
apm install --dry-run
```

`apm install` also deploys the project's own `.apm/` content (instructions, prompts, agents, skills, hooks, commands) to target directories alongside dependency content. Local content takes priority over dependencies on collision. This works even with zero dependencies -- just `apm.yml` and a `.apm/` directory is enough. See the [CLI reference](../../reference/cli-commands/#apm-install---install-dependencies-and-deploy-local-content) for details and exceptions.

### 3. Verify Installation

```bash
# List installed packages
apm deps list

# Show dependency tree
apm deps tree

# Get package details
apm view apm-sample-package
```

### 4. Use Dependencies in Compilation

```bash
# Compile with dependencies
apm compile

# Compilation generates distributed files across the project
# Instructions with matching applyTo patterns are merged from all sources
```

## Development Dependencies

Some packages are only needed during authoring — test fixtures, linting rules, internal helpers. Install them as dev dependencies so they stay out of distributed bundles:

```bash
apm install --dev owner/test-helpers
```

Or declare them directly:

```yaml
devDependencies:
  apm:
    - source: owner/test-helpers
```

Dev dependencies install to `apm_modules/` like production deps but are excluded from `apm pack --format plugin` output. See [Pack & Distribute](../pack-distribute/) for details.

## Local Path Dependencies

Install packages from the local filesystem for fast iteration during development.

```bash
# Relative path
apm install ./packages/my-shared-skills

# Absolute path
apm install /home/user/repos/my-ai-package
```

Or declare them in `apm.yml`:

```yaml
dependencies:
  apm:
    - ./packages/my-shared-skills          # relative to project root
    - /home/user/repos/my-ai-package       # absolute path
    - microsoft/apm-sample-package         # remote (can be mixed)
```

**How it works:**
- Files are **copied** (not symlinked) to `apm_modules/_local/<package-name>/`
- Local packages are validated the same as remote packages (must have `apm.yml` or `SKILL.md`)
- `apm compile` works identically regardless of dependency source
- Transitive dependencies are resolved recursively (local packages can depend on remote packages)

**Re-install behavior:** Local deps are always re-copied on `apm install` since there is no commit SHA to cache against. This ensures you always get the latest local changes.

**Lockfile representation:** Local dependencies are tracked with `source: local` and `local_path` fields. No `resolved_commit` is stored.

**Pack guard:** `apm pack` rejects packages with local path dependencies — replace them with remote references before distributing.

**User-scope guard:** Local path dependencies are **not supported** with `--global` (`-g`). Relative paths resolve against `cwd`, which is meaningless at user scope where packages deploy to `~/.apm/`. Use remote references (`owner/repo`) for global installs.

## Global (User-Scope) Installation

By default, `apm install` targets the **current project** -- manifest, modules, and lockfile live in
the working directory and deployed primitives go to `.github/`, `.claude/`, `.cursor/`, `.opencode/`.

Pass `--global` (or `-g`) to install to your **home directory** instead, making packages available
across every project on the machine:

```bash
apm install -g microsoft/apm-sample-package
apm uninstall -g microsoft/apm-sample-package
apm deps list -g       # user-scope packages only
apm deps list --all    # project + user-scope packages
```

| Item | Project scope (default) | User scope (`-g`) |
|------|------------------------|-------------------|
| Manifest | `./apm.yml` | `~/.apm/apm.yml` |
| Modules | `./apm_modules/` | `~/.apm/apm_modules/` |
| Lockfile | `./apm.lock.yaml` | `~/.apm/apm.lock.yaml` |
| Deployed primitives | `./.github/`, `./.claude/`, ... | `~/.copilot/`, `~/.claude/`, `~/.cursor/`, `~/.config/opencode/` |

### Per-target support

Coverage varies by target and primitive type:

| Target | Status | User-level dir | Primitives | Not supported |
|--------|--------|---------------|------------|---------------|
| Claude Code | Supported | `~/.claude/` | Skills, agents, commands, hooks, instructions | -- |
| Copilot CLI | Partial | `~/.copilot/` | Skills, agents, hooks | Prompts, instructions |
| Cursor | Partial | `~/.cursor/` | Skills, agents, hooks | Rules |
| OpenCode | Partial | `~/.config/opencode/` | Skills, agents, commands | Hooks |

Target detection mirrors project scope: APM auto-detects by `~/.<target>/` directory presence,
falling back to Copilot. Security scanning runs for global installs.

### When to use each scope

| Use case | Scope |
|----------|-------|
| Team-shared instructions and prompts | Project (`apm install`) |
| Personal commands, agents, or skills | User (`apm install -g`) |
| CI/CD reproducible setup | Project |
| Cross-project coding standards | User |

:::note
MCP servers at user scope (`--global`) are installed only to runtimes with global config paths (Copilot CLI, Codex CLI). Workspace-only runtimes (VS Code, Cursor, OpenCode) are skipped.
:::

:::caution
Local path dependencies (`./path`, `../path`, `/abs/path`) are rejected at user scope. Relative paths resolve against `cwd`, which differs from the user-scope deploy root (`~/.apm/`). Use remote references for `apm install -g`.
:::

## MCP Dependency Formats

MCP dependencies support three forms: string references, overlay objects, and self-defined servers.

### String Reference (default)

Registry-resolved by name. Simplest form:

```yaml
mcp:
  - io.github.github/github-mcp-server
```

### Object with Overlays

Customize a registry-resolved server with project-specific preferences:

```yaml
mcp:
  - name: io.github.github/github-mcp-server
    transport: stdio          # Prefer stdio over remote
    env:                      # Pre-populate environment variables
      GITHUB_TOKEN: "${MY_TOKEN}"
    tools: ["repos", "issues"]  # Restrict exposed tools
    headers:                  # Custom HTTP headers (remote transports)
      X-Custom: "value"
    package: npm              # Select package type (npm, pypi, oci)
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Server reference (required) |
| `transport` | string | `stdio`, `sse`, `http`, or `streamable-http` |
| `env` | dict | Environment variable overrides |
| `args` | list or dict | Runtime argument overrides |
| `version` | string | Pin server version |
| `package` | string | Select package type (`npm`, `pypi`, `oci`) |
| `headers` | dict | HTTP headers for remote transports |
| `tools` | list | Restrict exposed tool names |

Overlay fields are merged on top of registry metadata — they augment, never replace, the registry-first model.

### Self-Defined Servers (`registry: false`)

For private or corporate MCP servers not published to any registry:

```yaml
mcp:
  - name: internal-knowledge-base
    registry: false
    transport: http
    url: "https://mcp.internal.example.com"
    env:
      API_TOKEN: "${API_TOKEN}"
    headers:
      Authorization: "Bearer ${API_TOKEN}"
```

Stdio example:

```yaml
mcp:
  - name: local-db-tool
    registry: false
    transport: stdio
    command: my-mcp-server
    args:
      - "--port"
      - "8080"
```

**Required fields when `registry: false`:**
- `transport` — always required
- `url` — required for `http`, `sse`, `streamable-http` transports
- `command` — required for `stdio` transport

⚠️ **Transitive trust rule:** Self-defined servers from direct dependencies (depth=1 in the lockfile) are auto-trusted. Self-defined servers from transitive dependencies (depth > 1) are skipped with a warning by default. You can either re-declare them in your own `apm.yml`, or use `--trust-transitive-mcp` to trust all self-defined servers from upstream packages:

```bash
apm install --trust-transitive-mcp
```

### Validation

Run `apm install --dry-run` to preview MCP dependency configuration without writing any files. Self-defined deps are validated for required fields and transport values; overlay deps are loaded as-is and unknown fields are ignored.

## Transport selection (SSH vs HTTPS)

APM picks SSH or HTTPS per dependency using a strict, predictable contract.

:::caution[Breaking change in APM 0.8.13]
APM versions before 0.8.13 silently retried failed clones across protocols.
Starting in 0.8.13 the behavior is **strict by default**: explicit URL schemes are honored exactly,
and shorthand uses HTTPS unless `git config url.<base>.insteadOf` rewrites it
to SSH. To restore the legacy permissive chain temporarily (e.g. while
migrating CI), set `APM_ALLOW_PROTOCOL_FALLBACK=1` or pass
`--allow-protocol-fallback`.
:::

| Dependency form | What APM tries |
|-----------------|----------------|
| `ssh://...` or `git@host:...` | SSH only |
| `https://...` or `http://...` | HTTPS only |
| Shorthand (`owner/repo`, `host/owner/repo`) with `git config url.<base>.insteadOf` rewriting to SSH | SSH only |
| Shorthand without a matching `insteadOf` rewrite | HTTPS only |

A failed clone fails loudly, naming the URL and the protocol attempted. APM
no longer downgrades `ssh://` to HTTPS or vice-versa.

### Honoring `git config insteadOf`

If your machine rewrites HTTPS to SSH for a host, APM matches `git clone`'s
behavior on that machine. Example:

```bash
git config --global url."git@github.com:".insteadOf "https://github.com/"
apm install owner/repo        # APM clones over SSH
```

No CLI flag is needed. `insteadOf` is consulted only for shorthand
dependencies; explicit URLs in `apm.yml` are not rewritten.

### Forcing the initial protocol for shorthand

```bash
apm install owner/repo --ssh        # force SSH for shorthand
apm install owner/repo --https      # force HTTPS for shorthand
export APM_GIT_PROTOCOL=ssh         # session default
```

`--ssh` and `--https` are mutually exclusive and apply only to shorthand
dependencies. URLs with an explicit scheme ignore them.

### Restoring the legacy permissive chain

```bash
apm install --allow-protocol-fallback
export APM_ALLOW_PROTOCOL_FALLBACK=1   # CI / migration window
```

When fallback runs, each cross-protocol retry emits a `[!]` warning naming
both protocols. Use this to unblock a pipeline while you fix the root
cause -- not as a long-term setting.

For SSH key selection (ssh-agent, `~/.ssh/config`) and HTTPS token
resolution, see
[Authentication](../../getting-started/authentication/#choosing-transport-ssh-vs-https).
For the CLI flag and env var reference, see
[`apm install`](../../reference/cli-commands/#apm-install---install-dependencies-and-deploy-local-content).

## GitHub Authentication Setup

For GitHub and GitHub Enterprise repositories, set up a personal access token:

### Option 1: Fine-grained Token (Recommended)

Create a fine-grained personal access token at [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new):

- **Repository access**: Select specific repositories or "All repositories"
- **Permissions**: 
  - Contents: Read (to access repository files)
  - Metadata: Read (to access basic repository information)

```bash
export GITHUB_CLI_PAT=your_fine_grained_token
```

### Option 2: Classic Token (Fallback)

Create a classic personal access token with `repo` scope:

```bash
export GITHUB_TOKEN=your_classic_token
```

### Verify Authentication

```bash
# Test that your token works
apm install --dry-run
```

If authentication fails, you'll see an error with guidance on token setup.

### Other Git Hosts (GitLab, Bitbucket, etc.)

For non-GitHub repositories, APM delegates authentication to git — it never sends GitHub tokens to non-GitHub hosts:

- **Public repos**: Work without authentication via HTTPS
- **Private repos via SSH**: Configure SSH keys for your host. Use an `ssh://` or `git@host:` URL, or set up `git config url.<base>.insteadOf` to rewrite shorthand to SSH (see [Transport selection](#transport-selection-ssh-vs-https))
- **Private repos via HTTPS**: Configure a [git credential helper](https://git-scm.com/docs/gitcredentials) — APM allows credential helpers for non-GitHub hosts

```bash
# Ensure SSH keys are configured for your host
ssh -T git@gitlab.com
ssh -T git@bitbucket.org
```

## Real-World Example: Corporate Website Project

This example shows how APM dependencies enable powerful layered functionality by combining multiple specialized packages. The company website project uses [microsoft/apm-sample-package](https://github.com/microsoft/apm-sample-package) as a full APM package and individual prompts from [github/awesome-copilot](https://github.com/github/awesome-copilot) to supercharge development workflows:

```yaml
# company-website/apm.yml
name: company-website
version: 1.0.0
description: Corporate website with design standards and code review
dependencies:
  apm:
    - microsoft/apm-sample-package#v1.0.0
    - github/awesome-copilot/skills/review-and-refactor
  mcp:
    - io.github.github/github-mcp-server
    - name: internal-knowledge-base
      registry: false
      transport: http
      url: "${KNOWLEDGE_BASE_URL}"
      env:
        KB_TOKEN: "${KB_TOKEN}"

scripts:
  # Design workflows  
  design-review: "codex --skip-git-repo-check design-review.prompt.md"
  accessibility: "codex --skip-git-repo-check accessibility-audit.prompt.md"
```

### Package Contributions

The combined packages provide comprehensive coverage:

**[apm-sample-package](https://github.com/microsoft/apm-sample-package) contributes:**
- **Agent Workflows**: `.apm/prompts/design-review.prompt.md`, `.apm/prompts/accessibility-audit.prompt.md`
- **Instructions**: `.apm/instructions/design-standards.instructions.md` - Design guidelines
- **Agents**: `.apm/agents/design-reviewer.agent.md` - Design review persona
- **Skills**: `.apm/skills/style-checker/SKILL.md` - Style checking capability

**[github/awesome-copilot](https://github.com/github/awesome-copilot) virtual packages contribute:**
- **Prompts**: Individual prompt files installed via virtual package references

### Compounding Benefits

When both packages are installed, your project gains:
- **Accessibility audit** capabilities for web components
- **Design system enforcement** with automated style checking
- **Code review** workflows from community prompts
- **Rich context** about design standards

## Dependency Resolution

### Installation Process

1. **Parse Configuration**: APM reads the `dependencies.apm` section from `apm.yml`
2. **Download Repositories**: Clone or update each GitHub repository to `apm_modules/`
3. **Validate Packages**: Ensure each repository has valid APM package structure
4. **Build Dependency Graph**: Resolve transitive dependencies recursively
5. **Check Conflicts**: Identify any circular dependencies or conflicts

#### Resilient Downloads

APM automatically retries failed HTTP requests with exponential backoff and jitter. Rate-limited responses (HTTP 429/503) are handled transparently, respecting `Retry-After` headers when provided. This ensures reliable installs even under heavy API usage or transient network issues.

#### Parallel Downloads

APM downloads packages in parallel using a thread pool, significantly reducing wall-clock time for large dependency trees. The concurrency level defaults to 4 and is configurable via `--parallel-downloads` (set to 0 to disable). For subdirectory packages in monorepos, APM attempts git sparse-checkout (git 2.25+) to download only the needed directory, falling back to a shallow clone if sparse-checkout is unavailable.

### File Processing and Content Merging

APM uses instruction-level merging rather than file-level precedence. When local and dependency files contribute instructions with overlapping `applyTo` patterns:

```
my-project/
├── .apm/
│   └── instructions/
│       └── security.instructions.md      # Local instructions (applyTo: "**/*.py")
├── apm_modules/
│   └── compliance-rules/
│       └── .apm/
│           └── instructions/
│               └── compliance.instructions.md  # Dependency instructions (applyTo: "**/*.py")
└── apm.yml
```

During compilation, APM merges instruction content by `applyTo` patterns:
1. **Pattern-Based Grouping**: Instructions are grouped by their `applyTo` patterns, not by filename
2. **Content Merging**: All instructions matching the same pattern are concatenated in the final AGENTS.md
3. **Source Attribution**: Each instruction includes source file attribution when compiled

This allows multiple packages to contribute complementary instructions for the same file types, enabling rich layered functionality.

### Dependency Tree Structure

Based on the actual structure of our real-world examples:

```
my-project/
├── apm_modules/                     # Dependency installation directory
│   ├── microsoft/
│   │   └── apm-sample-package/      # From microsoft/apm-sample-package
│   │       ├── .apm/
│   │       │   ├── instructions/
│   │       │   │   └── design-standards.instructions.md
│   │       │   ├── prompts/
│   │       │   │   ├── design-review.prompt.md
│   │       │   │   └── accessibility-audit.prompt.md
│   │       │   ├── agents/
│   │       │   │   └── design-reviewer.agent.md
│   │       │   └── skills/
│   │       │       └── style-checker/SKILL.md
│   │       └── apm.yml
│   └── github/
│       └── awesome-copilot/              # Virtual subdirectory from github/awesome-copilot
│           └── skills/
│               └── review-and-refactor/
│                   ├── SKILL.md
│                   └── apm.yml
├── .apm/                            # Local context (highest priority)
├── apm.yml                          # Project configuration
└── .gitignore                       # Manually add apm_modules/ to ignore
```

**Note**: Full APM packages store primitives under `.apm/` subdirectories. Virtual file packages extract individual files from monorepos like `github/awesome-copilot`.

## Advanced Scenarios

### Branch and Tag References

Specify specific branches, tags, or commits for dependency versions:

```yaml
dependencies:
  apm:
    - github/awesome-copilot/skills/review-and-refactor#v2.1.0    # Specific tag
    - microsoft/apm-sample-package#main     # Specific branch  
    - company/internal-standards#abc123        # Specific commit
```

### Updating Dependencies

```bash
# Update all dependencies to latest refs
apm deps update

# Update specific dependency (use the owner/repo form from apm.yml)
apm deps update owner/apm-sample-package

# Update with verbose output
apm deps update --verbose

# Update user-scope dependencies
apm deps update -g

# Install with updates (equivalent to update)
apm install --update
```

## Reproducible Builds with apm.lock.yaml

APM generates a lockfile (`apm.lock.yaml`) after each successful install to ensure reproducible builds across machines and CI environments.

### What is apm.lock.yaml?

The `apm.lock.yaml` file captures the exact state of your dependency tree, including which files APM deployed:

```yaml
lockfile_version: "1.0"
generated_at: "2026-01-22T10:30:00Z"
apm_version: "0.8.0"
dependencies:
  microsoft/apm-sample-package:
    repo_url: "https://github.com/microsoft/apm-sample-package"
    resolved_commit: "abc123def456"
    resolved_ref: "main"
    version: "1.0.0"
    depth: 1
    deployed_files:
      - .github/prompts/design-review.prompt.md
      - .github/prompts/accessibility-audit.prompt.md
      - .github/agents/design-reviewer.agent.md
  contoso/validation-patterns:
    repo_url: "https://github.com/contoso/validation-patterns"
    resolved_commit: "789xyz012"
    resolved_ref: "main"
    version: "1.2.0"
    depth: 2
    resolved_by: "microsoft/apm-sample-package"
mcp_servers:
  - acme-kb
  - github
```

The `deployed_files` field tracks exactly which files APM placed in your project. This enables safe cleanup on `apm uninstall` and `apm prune` — only tracked files are removed.

The `mcp_servers` field records the MCP dependency references (e.g. `io.github.github/github-mcp-server`) for servers currently managed by APM. It is used to detect and clean up stale servers when dependencies change.

### How It Works

1. **First install**: APM resolves dependencies, downloads packages, and writes `apm.lock.yaml`
2. **Subsequent installs**: APM reads `apm.lock.yaml` and uses locked commits for exact reproducibility. If the local checkout already matches the locked commit SHA, the download is skipped entirely.
3. **Updating**: Use `--update` to re-resolve dependencies and generate a fresh lockfile. This re-resolves all dependencies, including transitive ones, so stale locked SHAs are never reused.

### Version Control

**Commit `apm.lock.yaml`** to version control:

```bash
git add apm.lock.yaml
git commit -m "Lock dependencies"
```

This ensures all team members and CI pipelines get identical dependencies.

### Forcing Re-resolution

When you want the latest versions (ignoring the lockfile):

```bash
# Re-resolve all dependencies and update lockfile
apm install --update
```

### Transitive Dependencies

APM fully resolves transitive dependencies. If package A depends on B, and B depends on C:

```
apm install contoso/package-a
```

Result:
- Downloads A, B, and C
- Records all three in `apm.lock.yaml` with depth information
- `depth: 1` = direct dependency
- `depth: 2+` = transitive dependency

Uninstalling a package also removes its orphaned transitive dependencies (npm-style pruning).
You can use any input form — APM resolves it to the canonical identity stored in `apm.yml`:

```bash
apm uninstall acme/package-a
apm uninstall https://github.com/acme/package-a.git   # same effect
apm uninstall git@github.com:acme/package-a.git        # same effect
# Also removes B and C if no other package depends on them
```

### Cleaning Dependencies

```bash
# Remove all APM dependencies
apm deps clean

# This removes the entire apm_modules/ directory
# Use with caution - requires reinstallation
```

## Best Practices

### Package Structure

Create well-structured APM packages for maximum reusability:

```
your-package/
├── .apm/
│   ├── instructions/        # Context for AI behavior
│   ├── contexts/           # Domain knowledge and facts  
│   ├── chatmodes/          # Interactive chat configurations
│   └── prompts/            # Agent workflows
├── apm.yml                 # Package metadata
├── README.md               # Package documentation
└── examples/               # Usage examples (optional)
```

### Package Naming

- Use descriptive, specific names: `compliance-rules`, `design-guidelines`
- Follow GitHub repository naming conventions
- Consider organization/team prefixes: `company/platform-standards`

### Version Management

- Use semantic versioning for package releases
- Tag releases for stable dependency references
- Document breaking changes clearly

### Documentation

- Include clear README.md with usage examples
- Document all prompts and their parameters
- Provide integration examples

## Troubleshooting

### Common Issues

#### "Authentication failed" 
**Problem**: GitHub token is missing or invalid
**Solution**: 
```bash
# Verify token is set
echo $GITHUB_CLI_PAT

# Test token access
curl -H "Authorization: token $GITHUB_CLI_PAT" https://api.github.com/user
```

#### "Package validation failed"
**Problem**: Repository doesn't have valid APM package structure
**Solution**: 
- Ensure target repository has `.apm/` directory
- Check that `apm.yml` exists and is valid
- Verify repository is accessible with your token

#### "Circular dependency detected"
**Problem**: Packages depend on each other in a loop
**Solution**:
- Review your dependency chain
- Remove circular references
- Consider merging closely related packages

#### "File conflicts during installation"
**Problem**: Local files collide with package files during `apm install`
**Resolution**: APM skips files that exist locally and aren't managed by APM. The diagnostic summary at the end of install shows how many files were skipped. Use `--verbose` to see which files, or `--force` to overwrite.

#### "File conflicts during compilation"
**Problem**: Multiple packages or local files have same names
**Resolution**: Local files automatically override dependency files with same names

### Getting Help

```bash
# Show detailed package information
apm view package-name

# Show full dependency tree
apm deps tree

# Preview installation without changes
apm install --dry-run

# See detailed diagnostics (skipped files, errors)
apm install --verbose

# Enable verbose logging for compilation
apm compile --verbose
```

## Integration with Workflows

### Continuous Integration

Add dependency installation to your CI/CD pipelines:

```yaml
# .github/workflows/apm.yml
- name: Install APM dependencies
  run: |
    apm install --only=apm
    apm compile
```

### Team Development

1. **Share dependencies** through your `apm.yml` file in version control
2. **Pin specific versions** for consistency across team members
3. **Document dependency choices** in your project README
4. **Update together** to avoid version conflicts

### Local Development

```bash
# Quick setup for new team members
git clone your-project
cd your-project
apm install
apm compile

# Now all team contexts and workflows are available
```

## Next Steps

- **[CLI Reference](../../reference/cli-commands/)** - Complete command documentation
- **[Getting Started](../../getting-started/installation/)** - Basic APM usage
- **[Context Guide](../../introduction/how-it-works/)** - Understanding the AI-Native Development framework
- **[Creating Packages](../../introduction/key-concepts/)** - Build your own APM packages

Ready to create your own APM packages? See the [Context Guide](../../introduction/key-concepts/) for detailed instructions on building reusable context collections and agent workflows.
