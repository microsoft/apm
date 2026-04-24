---
title: "Lock File Specification"
description: "The apm.lock.yaml format — how APM pins dependencies to exact versions for reproducible installs."
sidebar:
  order: 3
---

<dl>
<dt>Version</dt><dd>0.1 (Working Draft)</dd>
<dt>Date</dt><dd>2026-03-09</dd>
<dt>Editors</dt><dd>Daniel Meppiel (Microsoft)</dd>
<dt>Repository</dt><dd>https://github.com/microsoft/apm</dd>
<dt>Format</dt><dd>YAML 1.2</dd>
</dl>

## Status of This Document

This is a **Working Draft**. The lock file format is stable at version `"1"` and
breaking changes will be gated behind a `lockfile_version` bump.

## Abstract

`apm.lock.yaml` records the exact resolved state of every dependency in an APM
project. It is the receipt of what was installed — commit SHAs, source URLs,
and every file deployed into the workspace. Its role is analogous to
`package-lock.json` (npm) or `.terraform.lock.hcl` (Terraform): given the same
lock file, APM MUST reproduce the same file tree.

---

## 1. Conformance

The key words "MUST", "MUST NOT", "SHOULD", "SHOULD NOT", and "MAY" in this
document are to be interpreted as described in [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119).

## 2. Purpose

The lock file serves four goals:

1. **Reproducibility** — the same lock file yields the same deployed files on
   every machine, every time.
2. **Provenance** — every dependency is traceable to an exact source commit.
3. **Completeness** — `deployed_files` lists every file APM placed in the
   project, enabling precise removal.
4. **Auditability** — `git log apm.lock.yaml` provides a full history of dependency
   changes across the lifetime of the project.

## 3. Lifecycle

`apm.lock.yaml` is created and updated at well-defined points:

| Event | Effect on `apm.lock.yaml` |
|-------|----------------------|
| `apm install` (first run) | Created. All dependencies resolved, commits pinned, files recorded. |
| `apm install` (subsequent) | Read. Locked commits reused. New dependencies appended. |
| `apm install --update` | Re-resolved. All refs re-resolved to latest matching commits. |
| `apm deps update` | Re-resolved. Refreshes versions for specified or all dependencies. |
| `apm pack` | Enriched. A `pack:` section is prepended to the bundled copy (see [section 6](#6-pack-enrichment)). |
| `apm uninstall` | Updated. Removed dependency entries and their `deployed_files` references. |

The lock file SHOULD be committed to version control. It MUST NOT be
manually edited — APM is the sole writer.

## 4. Document Structure

A conforming lock file MUST be a YAML 1.2 document with the following
top-level structure:

```yaml
lockfile_version: "1"
generated_at: "2026-03-09T14:00:00Z"
apm_version: "0.7.7"

dependencies:
  - repo_url: https://github.com/acme-corp/security-baseline
    resolved_commit: a1b2c3d4e5f6789012345678901234567890abcd
    resolved_ref: v2.1.0
    version: "2.1.0"
    depth: 1
    package_type: apm_package
    deployed_files:
      - .github/instructions/security.instructions.md
      - .github/agents/security-auditor.agent.md

  - repo_url: https://github.com/acme-corp/common-prompts
    resolved_commit: f6e5d4c3b2a1098765432109876543210fedcba9
    resolved_ref: main
    depth: 2
    resolved_by: https://github.com/acme-corp/security-baseline
    package_type: apm_package
    deployed_files:
      - .github/instructions/common-guidelines.instructions.md
```

### 4.1 Top-Level Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `lockfile_version` | string | MUST | Lock file format version. Currently `"1"`. |
| `generated_at` | string (ISO 8601) | MUST | UTC timestamp of when the lock file was last written. |
| `apm_version` | string | MUST | Version of APM that generated this lock file. |
| `dependencies` | array | MUST | Ordered list of resolved dependencies (see [section 4.2](#42-dependency-entries)). |
| `mcp_servers` | array | MAY | List of MCP server identifiers registered by installed packages. |
| `mcp_configs` | mapping | MAY | Mapping of MCP server name to its manifest configuration dict. Used for diff-aware installation — when config in `apm.yml` changes, `apm install` detects the drift and re-applies without `--force`. |

### 4.2 Dependency Entries

The `dependencies` list MUST be sorted by `depth` (ascending), then by
`repo_url` (lexicographic). Each entry is a YAML mapping with the following
fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `repo_url` | string | MUST | Source repository URL, or `_local/<name>` for local path dependencies. |
| `host` | string | MAY | Git host identifier (e.g., `github.com`). Omitted when inferrable from `repo_url`. |
| `resolved_commit` | string | MUST (remote) | Full 40-character commit SHA that was checked out. Required for remote (git) dependencies; MUST be omitted for local (`source: "local"`) dependencies. |
| `resolved_ref` | string | MUST (remote) | Git ref (tag, branch, SHA) that resolved to `resolved_commit`. Required for remote (git) dependencies; MUST be omitted for local (`source: "local"`) dependencies. |
| `version` | string | MAY | Semantic version of the package, if declared in its manifest. |
| `virtual_path` | string | MAY | Sub-path within the repository for virtual (monorepo) packages. |
| `is_virtual` | boolean | MAY | `true` if the package is a virtual sub-package. Omitted when `false`. |
| `depth` | integer | MUST | Dependency depth. `1` = direct dependency, `2`+ = transitive. |
| `resolved_by` | string | MAY | `repo_url` of the parent that introduced this transitive dependency. Present only when `depth >= 2`. |
| `package_type` | string | MUST | Package type: `apm_package`, `plugin`, `virtual`, or other registered types. |
| `content_hash` | string | MAY | SHA-256 hash of the package file tree, in the format `"sha256:<hex>"`. Used to verify cached packages on subsequent installs. Omitted for local path dependencies. See [section 4.4](#44-content-integrity). |
| `is_dev` | boolean | MAY | `true` if the dependency was resolved through [`devDependencies`](../manifest-schema/#5-devdependencies). Omitted when `false`. Dev deps are excluded from `apm pack --format plugin` bundles. |
| `deployed_files` | array of strings | MUST | Every file path APM deployed for this dependency, relative to project root. |
| `source` | string | MAY | Dependency source. `"local"` for local path dependencies. Omitted for remote (git) dependencies. |
| `local_path` | string | MAY | Filesystem path (relative or absolute) to the local package. Present only when `source` is `"local"`. |
| `is_insecure` | boolean | MAY | `true` when the dep was fetched over HTTP (unencrypted). Omitted when `false`. Presence forces re-approval on the next install: the apm.yml entry MUST carry `allow_insecure: true` and the invocation MUST pass `--allow-insecure` (or `--allow-insecure-host` for transitive deps). Absent or `false` means HTTPS/SSH. |
| `allow_insecure` | boolean | MAY | `true` when the user's manifest explicitly approved the HTTP fetch with `allow_insecure: true`. Persisted alongside `is_insecure` for replay safety: a legacy lockfile with `is_insecure: true` but no `allow_insecure` fail-closes to `allow_insecure: false`, forcing re-approval. Omitted when `false`. |

Fields with empty or default values (empty strings, `false` booleans, empty
lists) SHOULD be omitted from the serialized output to keep the file concise.

**Dev dependency tracking:** Packages installed via `apm install --dev` are marked with `is_dev: true`. When building plugin bundles (`apm pack --format plugin`), dev dependencies are excluded from the output. Resolvers and CI tools should respect this flag when producing distributable artifacts.

### 4.3 Unique Key

Each dependency is uniquely identified by its `repo_url`, or by the
combination of `repo_url` and `virtual_path` for virtual packages.
For local path dependencies (`source: "local"`), the unique key is the
`local_path` value. A conforming lock file MUST NOT contain duplicate
entries for the same key.

### 4.4 Content Integrity

APM computes a SHA-256 hash of each package's file tree after download and stores
it as `content_hash` in the lock file. On subsequent installs, cached packages are
verified against this hash. A mismatch triggers a warning and re-download.

The hash covers all regular files sorted by POSIX path (deterministic regardless of
filesystem ordering). `.git/` and `__pycache__/` directories are excluded.

```yaml
dependencies:
  - repo_url: https://github.com/acme-corp/security-baseline
    resolved_commit: a1b2c3d4e5f6789012345678901234567890abcd
    content_hash: "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    # ...
```

Lock files generated before this feature omit `content_hash`. APM handles this
gracefully — verification is skipped and the hash is populated on the next install.

### 4.5 Self-Entry Convention

For uniform traversal, the in-memory `dependencies` map includes a synthesized
entry representing the host project's own local `.apm/` content. This entry is
materialized on read and stripped on write -- it is **never serialized** to disk.

The on-disk YAML format is unchanged: the host project's local content lives in
the flat top-level fields `local_deployed_files` and `local_deployed_file_hashes`
(see [section 4.4](#44-content-integrity) for the hashing scheme used for
verification). `LockFile.from_yaml()` synthesizes the self-entry from those
fields; `LockFile.to_yaml()` removes it before serialization. Round-trip is
byte-stable.

The synthesized entry MUST follow this convention:

| Field | Value |
|-------|-------|
| Map key | `"."` (single dot) |
| `repo_url` | `"<self>"` |
| `local_path` | `"."` |
| `source` | `"local"` |
| `is_dev` | `true` |
| `depth` | `0` |
| `deployed_files` | populated from `local_deployed_files` |
| `deployed_file_hashes` | populated from `local_deployed_file_hashes` |

`is_dev: true` is non-negotiable. Plugin bundle exporters (`apm pack --format
plugin`) skip dev dependencies; this flag ensures the host project's own content
is excluded from distributable bundles via the existing dev-dependency filter,
without requiring exporters to special-case the self-entry.

Consumers iterating `dependencies` SHOULD treat the `"."` key as the host
project. Consumers reading the on-disk YAML directly will not see this entry --
they MUST read `local_deployed_files` and `local_deployed_file_hashes` instead.

## 5. Path Conventions

All paths in `deployed_files` MUST use forward slashes (POSIX format),
regardless of the host operating system. Paths are relative to the project
root directory.

```yaml
# Correct
deployed_files:
  - .github/instructions/security.instructions.md
  - .github/agents/code-review.agent.md

# Incorrect — backslashes are not permitted
deployed_files:
  - .github\instructions\security.instructions.md
```

This convention ensures lock files are portable across operating systems and
produce consistent diffs in version control.

## 6. Pack Enrichment

When `apm pack` creates a bundle, it prepends a `pack:` section to the lock
file copy included in the bundle. This section is informational and is not
written back to the project's `apm.lock.yaml`.

```yaml
pack:
  format: apm
  target: vscode
  packed_at: "2026-03-09T14:30:00Z"

lockfile_version: "1"
generated_at: "2026-03-09T14:00:00Z"
# ... rest of lock file
```

### 6.1 Pack Fields

| Field | Type | Description |
|-------|------|-------------|
| `pack.format` | string | Bundle format: `"apm"` or `"plugin"`. |
| `pack.target` | string | Target environment: `"vscode"`, `"claude"`, or `"all"`. |
| `pack.packed_at` | string (ISO 8601) | UTC timestamp of when the bundle was created. |

The original lock file is not mutated. The enriched copy exists only inside the
packed archive.

## 7. Resolver Behaviour

The dependency resolver interacts with the lock file as follows:

1. **First install** — resolve all refs to commits, write `apm.lock.yaml`.
2. **Subsequent installs** — read `apm.lock.yaml`, reuse locked commits. Only
   newly added dependencies trigger resolution.
3. **Update** (`--update` flag or `apm deps update`) -- re-resolve all refs
   to their latest commits. If a resolved commit matches the existing lock
   file entry and the local checkout is intact, the download is skipped.
   Otherwise, the package is re-fetched. The lock file is always refreshed.

When a locked commit is no longer reachable (force-pushed branch, deleted tag),
APM MUST report an error and refuse to install until the lock file is updated.

## 8. Migration

The lock file reader supports the following historical migrations:

- **`deployed_skills`** — renamed to `deployed_files`. If a lock file contains
  the legacy key, it is silently migrated on read. New lock files MUST use
  `deployed_files`.

- **`apm.lock` → `apm.lock.yaml`** — the lock file was renamed from `apm.lock`
  to `apm.lock.yaml` (for IDE syntax highlighting). On the next `apm install`,
  an existing `apm.lock` is automatically renamed to `apm.lock.yaml` when the
  new file does not yet exist. The bundle unpacker also falls back to `apm.lock`
  when reading older bundles.

## 9. Auditing Patterns

Because `apm.lock.yaml` is committed to version control, standard Git operations
provide a complete audit trail:

```bash
# Full history of dependency changes
git log --oneline apm.lock.yaml

# What changed in the last commit
git diff HEAD~1 -- apm.lock.yaml

# State of dependencies at a specific release
git show v4.2.1:apm.lock.yaml

# Who last modified the lock file
git log -1 --format='%an <%ae> %ai' -- apm.lock.yaml
```

In CI pipelines, `apm audit --ci` verifies lockfile consistency (exit 0 = pass,
1 = fail). Add `--policy org` for organizational policy enforcement.

### 9.1 SOC 2 evidence

The lock file is the system of record for "what configuration was active when".
Three SOC 2-relevant questions answered directly from git:

- **Change authorization.** Every change to `apm.lock.yaml` is reviewed in a pull
  request before merge. The PR record is the change-authorization evidence.
- **Change history.** `git log apm.lock.yaml` produces a complete, tamper-evident
  history in git of every dependency change with author, timestamp, and commit message.
- **Point-in-time state.** `git show <ref>:apm.lock.yaml` reproduces the exact
  dependency set active at any tag, branch, or commit -- including past releases.

### 9.2 Security audit / incident forensics

When a vulnerability is disclosed in a dependency or a security incident requires
identifying which environments were exposed:

```bash
# Was the vulnerable package ever in the lock file?
git log -p apm.lock.yaml | grep -B2 -A2 "vulnerable-package"

# Which release included the vulnerable version?
git log --all --oneline -S 'vulnerable-package' -- apm.lock.yaml

# What is the current state of the dependency in production?
git show production:apm.lock.yaml | grep -A5 "vulnerable-package"
```

### 9.3 Change management pipeline

The lockfile-as-audit-trail model maps onto a standard 5-step change management
pipeline:

1. **Declaration** -- developer edits `apm.yml` and opens a PR.
2. **Resolution** -- `apm install` updates `apm.lock.yaml` with pinned versions.
3. **Review** -- PR reviewers see the manifest and lockfile diff together.
4. **Verification** -- CI runs `apm audit --ci` to confirm consistency and
   (optionally) policy compliance.
5. **Traceability** -- the merge commit becomes the durable record of the change,
   readable by every downstream environment via `apm install` from the same ref.

For organization-wide policy enforcement on top of this lockfile audit trail,
see [Governance](../../enterprise/governance-guide/).

## 10. Example: Complete Lock File

```yaml
lockfile_version: "1"
generated_at: "2026-03-09T14:00:00Z"
apm_version: "0.7.7"

dependencies:
  - repo_url: https://github.com/acme-corp/security-baseline
    resolved_commit: a1b2c3d4e5f6789012345678901234567890abcd
    resolved_ref: v2.1.0
    version: "2.1.0"
    depth: 1
    package_type: apm_package
    content_hash: "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    deployed_files:
      - .github/instructions/security.instructions.md
      - .github/agents/security-auditor.agent.md
      - .github/agents/threat-model.agent.md

  - repo_url: https://github.com/acme-corp/common-prompts
    resolved_commit: f6e5d4c3b2a1098765432109876543210fedcba9
    resolved_ref: main
    depth: 2
    resolved_by: https://github.com/acme-corp/security-baseline
    package_type: apm_package
    content_hash: "sha256:d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592"
    deployed_files:
      - .github/instructions/common-guidelines.instructions.md

  - repo_url: https://github.com/example-org/monorepo-tools
    host: github.com
    resolved_commit: 0123456789abcdef0123456789abcdef01234567
    resolved_ref: v1.0.0
    version: "1.0.0"
    virtual_path: packages/linter-config
    is_virtual: true
    depth: 1
    package_type: virtual
    deployed_files:
      - .github/instructions/linter.instructions.md

  - repo_url: https://github.com/acme-corp/test-helpers
    resolved_commit: abcdef1234567890abcdef1234567890abcdef12
    resolved_ref: main
    depth: 1
    package_type: apm_package
    is_dev: true
    content_hash: "sha256:4a44dc15364204a80fe80e9039455cc1608281820fe2b24f1e5233ade6af1dd5"
    deployed_files:
      - .github/instructions/test-helpers.instructions.md

mcp_servers:
  - security-scanner

mcp_configs:
  security-scanner:
    name: security-scanner
    transport: stdio
```

---

## Appendix A: Revision History

| Version | Date | Changes |
|---------|------|---------|
| 0.1 | 2026-03-09 | Initial working draft. |
