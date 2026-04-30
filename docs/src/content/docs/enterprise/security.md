---
title: "Security Model"
description: "How APM handles supply chain security for AI agents — attack surface boundaries, content scanning, dependency provenance, path safety, and MCP trust."
sidebar:
  order: 4
---

This page documents APM's security posture for enterprise security reviews, compliance audits, and supply chain assessments.

## The prompt supply chain is different

Traditional package managers install code that sits inert until a developer or CI pipeline explicitly executes it. Between `npm install` and `npm start`, there is a gap — time for `npm audit`, code review, and policy checks.

**Agent configuration has no such gap.** The moment a skill, instruction, or prompt file lands in `.github/prompts/` or `.claude/agents/`, any IDE agent watching the filesystem — Copilot, Cursor, Claude Code — may already be ingesting it. There is no "execution step." File presence IS execution.

This changes the security model fundamentally. APM treats package deployment as a **pre-deployment gate**: scan first, deploy only if clean.

## What APM does

APM is a build-time dependency manager for AI agent configuration. It performs four operations:

1. **Resolves git repositories** — clones or sparse-checks-out packages from GitHub or Azure DevOps.
2. **Deploys static files** — copies markdown, JSON, and YAML files into project directories (`.github/`, `.claude/`, `.cursor/`, `.opencode/`).
3. **Generates compiled output** — produces `AGENTS.md`, `CLAUDE.md`, and similar files from templates and prompts.
4. **Records a lock file** — writes `apm.lock.yaml` with exact commit SHAs for every resolved dependency.

## What APM does NOT do

APM has no runtime footprint. Once `apm install` or `apm compile` completes, the process exits.

- **No runtime component.** APM generates files then terminates. It does not run alongside your application.
- **No network calls after install.** All network activity (git clone/fetch) occurs during dependency resolution. There are no callbacks, webhooks, or phone-home requests.
- **No arbitrary code execution.** APM does not execute scripts from packages, evaluate expressions in templates, or run downloaded code.
- **No access to application data.** APM never reads databases, API responses, application state, or user data.
- **No persistent background processes.** APM does not install daemons, services, or scheduled tasks.
- **No telemetry or data collection.** APM collects no usage data, analytics, or diagnostics. Nothing is transmitted to Microsoft or any third party.

## Dependency provenance

APM resolves dependencies directly from git repositories. There is no intermediary registry, proxy, or mirror.

### Exact commit pinning

Every resolved dependency is recorded in `apm.lock.yaml` with its full commit SHA:

```yaml
lockfile_version: "1"
dependencies:
  - repo_url: owner/repo
    host: github.com
    resolved_commit: a1b2c3d4e5f6...
    resolved_ref: main
    depth: 1
    deployed_files:
      - .github/skills/example/skill.md
```

The `resolved_commit` field is a full 40-character SHA, not a branch name or tag. Subsequent `apm install` calls resolve to the same commit unless the lock file is explicitly updated.

### No registry

APM does not use a package registry. Dependencies are specified as git repository URLs in `apm.yml`. This eliminates the registry compromise vector entirely — there is no centralized service that can be poisoned to redirect installs.

### HTTP (insecure) dependencies

APM supports `http://` git dependencies for private mirrors and air-gapped
environments, but only behind explicit approval on both the manifest and CLI
surfaces:

- `allow_insecure: true` on the dependency entry records that the project
  intentionally permits HTTP for that dependency.
- `apm install --allow-insecure` approves direct HTTP dependencies for the
  current install run.
- Transitive HTTP dependencies inherit approval only when they come from the
  same host as an approved direct HTTP dependency. Additional transitive hosts
  require `--allow-insecure-host HOSTNAME`.

These controls make the decision visible, but they do **not** make HTTP safe:

- HTTP has no transport encryption or server authentication. A machine-in-the-middle can modify repository contents or refs in transit.
- On the first HTTP fetch (or any update fetched over HTTP), the lockfile's `resolved_commit` and `content_hash` come from that same untrusted channel. They improve replay detection later, but they do not establish trustworthy provenance for the initial fetch.
- APM explicitly suppresses git credential helpers for HTTP clone and `ls-remote` operations so stored tokens from Keychain, Credential Manager, `gh auth`, or other helpers are not sent over plaintext HTTP.

For routing all dependency traffic through an enterprise proxy (Artifactory or compatible), see [Registry Proxy & Air-gapped](../registry-proxy/).

## Content scanning

### The threat

Researchers have found hidden Unicode characters embedded in popular shared rules files. Tag characters (U+E0001–E007F) map 1:1 to invisible ASCII. Bidirectional overrides can reorder visible text. Zero-width joiners create invisible gaps. Variation selectors attach to visible characters, embedding invisible payload bytes that AST-based tools cannot detect. The Glassworm campaign (2026) exploited this mechanism to compromise repositories and VS Code extensions. LLMs tokenize all of these individually, meaning models process instructions that developers cannot see on screen.

### What APM detects

| Severity | Characters | Risk |
|----------|-----------|------|
| Critical | Tag characters (U+E0001–E007F), bidi overrides (U+202A–E, U+2066–9) | Hidden instruction embedding. Zero legitimate use in prompt files. |
| Critical | Variation selectors 17–256 (U+E0100–E01EF) | Glassworm attack vector — invisible payload encoding. Zero legitimate use in prompt files. |
| Warning | Zero-width spaces/joiners (U+200B–D), mid-file BOM (U+FEFF) | Common copy-paste debris, but can hide content. ZWJ inside emoji sequences is downgraded to info. |
| Warning | Variation selectors 1–15 (U+FE00–FE0E) | CJK typography / text presentation selectors. Uncommon in prompt files. |
| Warning | Bidi marks (U+200E–F, U+061C) | Invisible directional marks. No legitimate use in prompt files. |
| Warning | Invisible operators (U+2061–4) | Zero-width math operators. No legitimate use in prompt files. |
| Warning | Annotation markers (U+FFF9–B) | Interlinear annotation delimiters that can hide text. |
| Warning | Deprecated formatting (U+206A–F) | Deprecated since Unicode 3.0, invisible. |
| Info | Non-breaking spaces (U+00A0), unusual whitespace (U+2000–200A) | Mostly harmless, flagged for awareness. |
| Info | Emoji presentation selector (U+FE0F) | Common with emoji, informational only. |

### Pre-deployment gate

During `apm install`, source files in `apm_modules/` are scanned **before** any integrator copies them to target directories:

```
download → scan source → block or deploy → report
```

- **Critical findings block deployment.** The package is downloaded and cached so you can inspect it (`apm_modules/owner/package/`), but nothing reaches agent-readable directories.
- **Warnings are non-blocking.** Zero-width characters are flagged in the diagnostics summary. Files are deployed normally.
- **`--force` overrides the block.** Consistent with existing collision semantics — an explicit "I know what I'm doing."
- **Multi-package installs continue.** A blocked package doesn't stop other packages from installing. After all packages are processed, `apm install` exits with code 1 if any package was blocked — failing the CI step.

### Compile and pack scanning

Content scanning extends beyond install:

- **`apm compile`** scans compiled output (AGENTS.md, CLAUDE.md, commands) before writing to disk. Critical findings cause `apm compile` to exit with code 1 after writing — defense-in-depth since source files were already scanned at install, but compilation assembles content from multiple sources.
- **`apm pack`** scans files before bundling. This catches hidden characters before a package is published, preventing authors from accidentally distributing tainted content.
- **`apm unpack`** scans bundle contents before deployment. This is a pre-deployment gate matching `apm install` — critical findings block deployment unless `--force` is used.

### On-demand scanning

`apm audit` scans deployed files or any arbitrary file, independent of the install flow:

```bash
apm audit                        # Scan all installed packages
apm audit --file .cursorrules    # Scan any file
apm audit --strip                # Remove hidden characters (preserves emoji)
apm audit --strip --dry-run      # Preview what --strip would remove
```

The `--file` flag is useful for inspecting files obtained outside APM — downloaded rules files, copy-pasted instructions, or files from pull requests.

For CI pipelines, `apm audit` supports SARIF, JSON, and Markdown output:

```bash
apm audit -f sarif -o audit.sarif      # GitHub Code Scanning
apm audit -f json -o report.json       # Machine-readable
apm audit -f markdown -o report.md     # Step summaries
```

See [Content scanning with `apm audit`](../governance/#content-scanning-with-apm-audit) for usage details and exit codes.

### Limitations

Content scanning detects hidden Unicode characters. It does not detect:

- Plain-text prompt injection (visible but malicious instructions)
- Homoglyph substitution (visually similar characters from different scripts)
- Semantic manipulation (subtly misleading but syntactically normal text)
- Binary payload embedding

`--strip` removes dangerous and suspicious characters (critical and warning) from deployed copies while preserving legitimate content like emoji and whitespace. Zero-width joiners inside emoji sequences (e.g. 👨‍👩‍👧) are recognized and preserved. Use `--strip --dry-run` to preview what would be removed before modifying files. Strip does not modify the source package — the next `apm install` restores them. For persistent remediation, fix the upstream package or pin to a clean commit.

### Planned hardening

- **Hook transparency** — display hook script contents during install so developers can review what will execute.

## Content integrity hashing

APM computes a SHA-256 hash of each downloaded package's file tree and stores it in `apm.lock.yaml` as `content_hash`. On subsequent installs, cached packages are verified against the lockfile hash. A mismatch triggers a warning and re-download.

```yaml
# apm.lock.yaml
dependencies:
  - repo_url: https://github.com/acme-corp/security-baseline
    resolved_commit: a1b2c3d4e5f6...
    content_hash: "sha256:9f86d081884c7d659a2feaa0c55ad015..."
```

The hash is deterministic — computed over sorted file paths and contents, independent of filesystem metadata (timestamps, permissions). `.git/` and `__pycache__/` directories are excluded.

Lock files generated before this feature omit `content_hash`. APM handles this gracefully — verification is skipped and the hash is populated on the next install.

See the [Lock File Specification](../../reference/lockfile-spec/#44-content-integrity) for field details.

## Path security

APM deploys files only to controlled subdirectories within the project root.

### Path traversal prevention

All deploy paths are validated before any file operation:

1. **No `..` segments.** Any path containing `..` is rejected outright.
2. **Allowed prefixes only.** Paths must start with an allowed prefix (`.github/`, `.claude/`, `.cursor/`, or `.opencode/`).
3. **Resolution containment.** The fully resolved path must remain within the project root directory.

A path must pass all three checks. Failure on any check prevents the file from being written.

### Symlink handling

Symlinks are never followed during file discovery or artifact operations:

- **Primitive discovery** (instructions, agents, prompts, contexts, skills) rejects symlinked files during glob-based file enumeration. Symlinks are silently skipped.
- **Prompt resolution** (`apm preview`, `apm run`) rejects symlinked `.prompt.md` files with an explicit error message.
- **Integrator file discovery** (agents, instructions, prompts, skills, hooks) rejects symlinked files via `is_symlink()` checks in `find_files_by_glob` and `find_hook_files`.
- **Tree copy operations** skip symlinks entirely -- they are excluded from the copy via an ignore filter.
- **MCP configuration files** that are symlinks are rejected with a warning and not parsed.
- **Manifest parsing** requires files to pass both `.is_file()` and `not .is_symlink()` checks.
- **Manifest integrity** -- a malformed `apm.yml` (invalid YAML or non-mapping content) triggers a failing `manifest-parse` audit check. Policy and baseline CI checks never silently pass when the manifest cannot be parsed. If this check fires, fix the YAML syntax error in your `apm.yml` and re-run the audit.
- **Archive creation** -- `apm pack` excludes symlinks from bundled archives. Packaged artifacts contain no symbolic links, preventing symlink-based escape attacks in distributed bundles.

This prevents symlink-based attacks that could escape allowed directories or cause APM to read or write outside the project root.

### Collision detection

When APM deploys a file, it checks whether a file already exists at the target path:

- If the file is **tracked in the managed files set** (deployed by a previous APM install), it is overwritten.
- If the file is **not tracked** (user-authored or created by another tool), APM skips it and prints a warning.
- The `--force` flag overrides collision detection, allowing APM to overwrite untracked files.

### Development dependency isolation

APM separates production and development dependencies:

- **Production dependencies** (`dependencies.apm`) are included in plugin bundles and shared packages.
- **Development dependencies** (`devDependencies.apm`, installed via `apm install --dev`) are resolved and cached locally but **excluded** from `apm pack` output (both plugin format -- the default -- and `--format apm`).

This prevents transitive inclusion of development-only packages (test fixtures, linting rules, internal helpers) in distributed artifacts. The lockfile marks dev dependencies with `is_dev: true` for explicit tracking. See the [Lock File Specification](../../reference/lockfile-spec/#42-dependency-entries) for field details.

## MCP server trust model

APM integrates MCP (Model Context Protocol) server configurations from packages. Trust is explicit and scoped by dependency depth.

### Direct dependencies

MCP servers declared by your direct dependencies (packages listed in your `apm.yml`) are auto-trusted. You explicitly chose to depend on these packages, so their MCP server declarations are accepted.

### Transitive dependencies

MCP servers declared by transitive dependencies (dependencies of your dependencies) are **blocked by default**. Transitive MCP servers can request tool access, file system permissions, or network capabilities — blocking them ensures that adding a prompt package cannot silently grant MCP access to an unknown transitive dependency.

To allow transitive MCP servers, you must either:

- **Re-declare the dependency** in your own `apm.yml`, promoting it to a direct dependency.
- **Pass `--trust-transitive-mcp`** to explicitly opt in to transitive MCP servers for that install.

## Token handling

APM authenticates to git hosts using personal access tokens (PATs) read from environment variables.

| Purpose | Environment variables (checked in order) |
|---|---|
| GitHub packages | `GITHUB_APM_PAT`, `GITHUB_TOKEN`, `GH_TOKEN` |
| Azure DevOps packages | `ADO_APM_PAT` |

- **Never stored in files.** Tokens are read from the environment at runtime. They are never written to `apm.yml`, `apm.lock.yaml`, or any generated file.
- **Never logged.** Token values are not included in console output, error messages, or debug logs.
- **Scoped to their git host.** A GitHub token is only sent to GitHub. An Azure DevOps token is only sent to Azure DevOps. Tokens are never transmitted to any other endpoint.
- **Injected via transient git config.** APM passes credentials with `http.extraheader` for the duration of a single git invocation; tokens are never embedded in URLs and are not visible in `ps` or process listings.

For GitHub, a fine-grained PAT with read-only `Contents` permission on the repositories you depend on is sufficient.

### Azure DevOps AAD bearer tokens

When `ADO_APM_PAT` is unset, APM can authenticate to Azure DevOps with a Microsoft Entra ID bearer token issued on demand by the Azure CLI (`az account get-access-token`). The posture:

- **Short-lived.** Tokens expire in roughly 60 minutes, are acquired per resolution, and are never persisted by APM.
- **No new secrets in manifests.** Nothing is written to `apm.yml` or `apm.lock.yaml`. The token never crosses the `apm.yml`/lockfile boundary.
- **Compatible with managed-identity / service-account-only orgs.** Works in environments where PAT creation is disabled, including WIF-backed pipelines.
- **Same transport rules as PATs.** Bearer values are injected via `http.extraheader`, scoped to ADO hosts only, and never logged.

See [Authentication: AAD bearer tokens](../../getting-started/authentication/#authenticating-with-microsoft-entra-id-aad-bearer-tokens) for the resolution precedence and CI patterns.

## Attack surface comparison

| Vector | Traditional package manager | APM |
|---|---|---|
| Registry compromise | Attacker poisons central registry | No registry exists |
| Version substitution | Malicious version replaces legitimate one | Lock file pins exact commit SHA; content hash detects post-download tampering |
| Post-install scripts | Arbitrary code runs after install | No code execution |
| Typosquatting | Similar package names on registry | Dependencies are full git URLs |
| Build-time injection | Malicious build steps execute | No build step — files are copied |
| Hidden content injection | Not applicable (binary packages) | Pre-deploy scan blocks critical hidden Unicode; `apm audit` for on-demand checks |
| Compromised policy intermediary | Not applicable (no policy layer) | A malicious mirror or MITM returns valid YAML with relaxed rules. Mitigated by [`policy.hash` consumer-side pin](../policy-reference/#96-hash-pin-policyhash-consumer-side-verification) which verifies raw bytes against a project-pinned digest. |

## Frequently asked questions

### Can a package embed hidden instructions?

Not without detection. APM scans all package source files before deployment. Critical hidden characters (tag characters, bidi overrides) block deployment. `apm audit` provides on-demand scanning for any file, including those obtained outside APM.

### How do I audit what APM installed?

The `apm.lock.yaml` file records every dependency (with exact commit SHA) and every file deployed. It is a plain YAML file suitable for automated policy checks, diff review, and compliance tooling. See [Governance & Compliance](../governance/) for audit workflows.

### Is the APM binary signed?

APM is distributed as a PyPI package (`apm-cli`) and as pre-built binaries attached to GitHub Releases under the `microsoft` organization. Both distribution channels use GitHub Actions workflows with pinned dependencies and are auditable through the public repository.

### Where is the source code?

APM is open source under the MIT license, hosted on GitHub under the `microsoft` organization. The full source code, build pipeline, and release process are publicly auditable.
