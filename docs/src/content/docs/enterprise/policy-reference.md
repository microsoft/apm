---
title: Policy Reference
sidebar:
  order: 3
---

:::caution[Experimental Feature]
The `apm-policy.yml` schema is an early preview for testing and feedback. Fields, defaults, and inheritance semantics may change based on community input. Pin your policy to a specific APM version and monitor the [CHANGELOG](https://github.com/microsoft/apm/blob/main/CHANGELOG.md) for breaking changes.
:::

Complete reference for `apm-policy.yml` — the configuration file that defines organization-wide governance rules for APM packages.

## Schema overview

```yaml
name: "Contoso Engineering Policy"
version: "1.0.0"
extends: org                    # Optional: inherit from parent policy
enforcement: block              # warn | block | off

cache:
  ttl: 3600                     # Policy cache TTL in seconds

dependencies:
  allow: []                     # Allowed dependency patterns
  deny: []                      # Denied dependency patterns
  require: []                   # Required packages
  require_resolution: project-wins  # project-wins | policy-wins | block
  max_depth: 50                 # Max transitive dependency depth

mcp:
  allow: []                     # Allowed MCP server patterns
  deny: []                      # Denied MCP server patterns
  transport:
    allow: []                   # stdio | sse | http | streamable-http
  self_defined: warn            # deny | warn | allow
  trust_transitive: false       # Trust transitive MCP servers

compilation:
  target:
    allow: []                   # vscode | claude | cursor | opencode | codex | all
    enforce: null               # Enforce specific target (must be present in list)
  strategy:
    enforce: null               # distributed | single-file
  source_attribution: false     # Require source attribution

manifest:
  required_fields: []           # Required apm.yml fields
  scripts: allow                # allow | deny
  content_types:
    allow: []                   # instructions | skill | hybrid | prompts

unmanaged_files:
  action: ignore                # ignore | warn | deny
  directories: []               # Directories to monitor
```

## Top-level fields

### `name`

Human-readable policy name. Appears in audit output.

### `version`

Policy version string (e.g., `"1.0.0"`). Informational — not used for resolution.

### `enforcement`

Controls how violations are reported:

| Value | Behavior |
|-------|----------|
| `off` | Policy checks are skipped |
| `warn` | Violations are reported but do not fail the audit |
| `block` | Violations cause `apm audit --ci` to exit with code 1 |

### `extends`

Inherit from a parent policy. See [Inheritance](#inheritance).

| Value | Source |
|-------|--------|
| `org` | Parent org's `.github/apm-policy.yml` |
| `owner/repo` | Cross-org policy from a specific repository |
| `https://...` | Direct URL to a policy file |

---

## `cache`

### `ttl`

Time-to-live in seconds for the cached policy file. Default: `3600` (1 hour). The cache is stored in `apm_modules/.policy-cache/`.

---

## `dependencies`

Controls which packages repositories can depend on.

### `allow`

List of allowed dependency patterns. If non-empty, only matching dependencies are permitted.

```yaml
dependencies:
  allow:
    - "contoso/**"           # Any repo under contoso org
    - "contoso-eng/*"        # Any repo directly under contoso-eng
    - "third-party/approved" # Exact match
```

### `deny`

List of denied dependency patterns. Deny takes precedence over allow.

```yaml
dependencies:
  deny:
    - "untrusted-org/**"
    - "*/deprecated-*"
```

### `require`

Packages that must be present in every repository's `apm.yml`. Supports optional version pins:

```yaml
dependencies:
  require:
    - "contoso/agent-standards"           # Must be a dependency
    - "contoso/security-rules#v2.0.0"     # Must be at specific version
```

### `require_resolution`

Controls what happens when a required package's version conflicts with the repository's declared version:

| Value | Behavior |
|-------|----------|
| `project-wins` | Repository's declared version takes precedence |
| `policy-wins` | Policy's pinned version overrides the repository |
| `block` | Conflict causes a check failure |

### `max_depth`

Maximum allowed transitive dependency depth. Default: `50`. Set lower to limit supply chain depth:

```yaml
dependencies:
  max_depth: 3  # Direct + 2 levels of transitive
```

---

## `mcp`

Controls MCP (Model Context Protocol) server configurations.

### `allow` / `deny`

Pattern lists for MCP server names. Same glob syntax as dependency patterns.

```yaml
mcp:
  allow:
    - "github-*"
    - "internal-*"
  deny:
    - "untrusted-*"
```

### `transport.allow`

Restrict which transport protocols MCP servers can use:

```yaml
mcp:
  transport:
    allow:
      - stdio
      - streamable-http
```

Valid values: `stdio`, `sse`, `http`, `streamable-http`.

### `self_defined`

Controls MCP servers defined directly in a repository (not from packages):

| Value | Behavior |
|-------|----------|
| `allow` | Self-defined MCP servers are permitted |
| `warn` | Self-defined MCP servers trigger a warning |
| `deny` | Self-defined MCP servers fail the audit |

### `trust_transitive`

Whether to trust MCP servers declared by transitive dependencies. Default: `false`.

---

## `compilation`

### `target.allow` / `target.enforce`

Control which compilation targets are permitted. With multi-target support, these policies apply to every item in the target list:

- **`enforce`**: The enforced target must be present in the target list. Fails if missing (e.g., `enforce: vscode` requires `vscode` to appear in `target: [claude, vscode]`).
- **`allow`**: Every target in the list must be in the allowed set. Rejects any target not listed.

```yaml
compilation:
  target:
    allow: [vscode, claude]  # Only these targets allowed
    enforce: vscode           # Must be present in the target list
```

`enforce` takes precedence over `allow`. Use one or the other.

### `strategy.enforce`

Require a specific compilation strategy:

```yaml
compilation:
  strategy:
    enforce: distributed  # or: single-file
```

### `source_attribution`

Require source attribution in compiled output:

```yaml
compilation:
  source_attribution: true
```

---

## `manifest`

### `required_fields`

Fields that must be present and non-empty in every repository's `apm.yml`:

```yaml
manifest:
  required_fields:
    - version
    - description
```

### `scripts`

Whether the `scripts` section is allowed in `apm.yml`:

| Value | Behavior |
|-------|----------|
| `allow` | Scripts section is permitted |
| `deny` | Scripts section causes a check failure |

### `content_types.allow`

Restrict which content types packages can declare:

```yaml
manifest:
  content_types:
    allow:
      - instructions
      - skill
      - prompts
```

---

## `unmanaged_files`

Detect files in governance directories that are not tracked by APM.

### `action`

| Value | Behavior |
|-------|----------|
| `ignore` | Unmanaged files are not checked |
| `warn` | Unmanaged files trigger a warning |
| `deny` | Unmanaged files fail the audit |

### `directories`

Directories to scan for unmanaged files. Defaults:

```yaml
unmanaged_files:
  directories:
    - .github/agents
    - .github/instructions
    - .github/hooks
    - .cursor/rules
    - .claude
    - .opencode
```

---

## Pattern matching

Allow and deny lists use glob-style patterns:

| Pattern | Matches |
|---------|---------|
| `contoso/*` | `contoso/repo` but not `contoso/org/repo` |
| `contoso/**` | `contoso/repo`, `contoso/org/repo`, any depth |
| `*/approved` | `any-org/approved` |
| `exact/match` | Only `exact/match` |

`*` matches any characters within a single path segment (no `/`). `**` matches across any number of segments.

Deny patterns are evaluated first. If a reference matches any deny pattern, it fails regardless of the allow list. An empty allow list permits everything not denied.

---

## Check reference

### Baseline checks (always run with `--ci`)

| Check | Validates |
|-------|-----------|
| `lockfile-exists` | `apm.lock.yaml` is present when `apm.yml` declares dependencies |
| `ref-consistency` | Every dependency's manifest ref matches the lockfile's resolved ref |
| `deployed-files-present` | All files listed in lockfile `deployed_files` exist on disk |
| `no-orphaned-packages` | No lockfile packages are absent from the manifest |
| `config-consistency` | MCP server configs match lockfile baseline |
| `content-integrity` | Deployed files contain no critical hidden Unicode characters |

### Policy checks (run with `--ci --policy`)

**Dependencies:**

| Check | Validates |
|-------|-----------|
| `dependency-allowlist` | Every dependency matches the allow list |
| `dependency-denylist` | No dependency matches the deny list |
| `required-packages` | Every required package is in the manifest |
| `required-packages-deployed` | Required packages appear in lockfile with deployed files |
| `required-package-version` | Required packages with version pins match per `require_resolution` |
| `transitive-depth` | No dependency exceeds `max_depth` |

**MCP:**

| Check | Validates |
|-------|-----------|
| `mcp-allowlist` | MCP server names match the allow list |
| `mcp-denylist` | No MCP server matches the deny list |
| `mcp-transport` | MCP transport values are in the allowed list |
| `mcp-self-defined` | Self-defined MCP servers comply with policy |

**Compilation:**

| Check | Validates |
|-------|-----------|
| `compilation-target` | Compilation target matches policy |
| `compilation-strategy` | Compilation strategy matches policy |
| `source-attribution` | Source attribution is enabled if required |

**Manifest:**

| Check | Validates |
|-------|-----------|
| `required-manifest-fields` | All required fields are present and non-empty |
| `scripts-policy` | Scripts section absent if policy denies it |

**Unmanaged files:**

| Check | Validates |
|-------|-----------|
| `unmanaged-files` | No untracked files in governance directories |

---

## Inheritance

Policies can inherit from a parent using `extends`. This enables a three-level chain:

```
Enterprise hub → Org policy → Repo override
```

### Tighten-only merge rules

A child policy can only tighten constraints — never relax them:

| Field | Merge rule |
|-------|-----------|
| `enforcement` | Escalates: `off` < `warn` < `block` |
| `cache.ttl` | `min(parent, child)` |
| Allow lists | Intersection — child narrows parent's allowed set |
| Deny lists | Union — child adds to parent's denied set |
| `require` | Union — combines required packages |
| `require_resolution` | Escalates: `project-wins` < `policy-wins` < `block` |
| `max_depth` | `min(parent, child)` |
| `mcp.self_defined` | Escalates: `allow` < `warn` < `deny` |
| `manifest.scripts` | Escalates: `allow` < `deny` |
| `unmanaged_files.action` | Escalates: `ignore` < `warn` < `deny` |
| `source_attribution` | `parent OR child` — either enables it |
| `trust_transitive` | `parent AND child` — both must allow it |

The inheritance chain is limited to 5 levels. Cycles are detected and rejected.

### Example: repo override

```yaml
# Repo-level apm-policy.yml
name: "Frontend Team Policy"
version: "1.0.0"
extends: org  # Inherits org policy, can only tighten

dependencies:
  deny:
    - "legacy-org/**"  # Additional deny on top of org policy
```

---

## Examples

### Minimal: deny-only policy

```yaml
name: "Block Untrusted Sources"
version: "1.0.0"
enforcement: block

dependencies:
  deny:
    - "untrusted-org/**"
```

### Standard org policy

```yaml
name: "Contoso Engineering"
version: "1.0.0"
enforcement: block

dependencies:
  allow:
    - "contoso/**"
    - "contoso-oss/**"
  require:
    - "contoso/agent-standards"
  max_depth: 5

mcp:
  deny:
    - "untrusted-*"
  transport:
    allow: [stdio, streamable-http]
  self_defined: warn

manifest:
  required_fields: [version, description]

unmanaged_files:
  action: warn
```

### Enterprise hub with inheritance

```yaml
# Enterprise hub: enterprise-org/.github/apm-policy.yml
name: "Enterprise Baseline"
version: "2.0.0"
enforcement: block

dependencies:
  deny:
    - "banned-org/**"
  max_depth: 10

mcp:
  self_defined: deny
  trust_transitive: false

manifest:
  scripts: deny
```

```yaml
# Org policy: contoso/.github/apm-policy.yml
name: "Contoso Policy"
version: "1.0.0"
extends: "enterprise-org/.github"  # Inherits enterprise baseline

dependencies:
  allow:
    - "contoso/**"
  require:
    - "contoso/agent-standards"
  max_depth: 5  # Tightens from 10 to 5
```

## Related

- [Governance & Compliance](../../enterprise/governance/) -- conceptual overview of APM's governance model
- [CI Policy Enforcement](../../guides/ci-policy-setup/) -- step-by-step CI setup tutorial
- [GitHub Rulesets](../../integrations/github-rulesets/) -- enforce policy as a required status check
