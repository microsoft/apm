---
title: Policy Reference
sidebar:
  order: 8
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
fetch_failure: warn             # warn | block, default warn (org-side knob; see Section 9.5)

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
| `block` | Violations abort `apm install` (exit 1) AND fail `apm audit --ci` |

### `extends`

Inherit from a parent policy. See [Inheritance](#inheritance).

| Value | Source |
|-------|--------|
| `org` | Parent org's `.github/apm-policy.yml` |
| `owner/repo` | Cross-org policy from a specific repository |
| `https://...` | Direct URL to a policy file |

### `fetch_failure`

Org-side posture when consumers cannot fetch this policy AND have a stale cached copy. Optional. Default: `warn`.

| Value | Behavior |
|-------|----------|
| `warn` | Loud warning emitted; install proceeds with the cached policy (or with no policy if cache is empty). Default. |
| `block` | Fail-closed when a cached policy is available but a refresh fails. |

Consumers can opt into fail-closed semantics for the no-cache case from their `apm.yml` via `policy.fetch_failure_default: block` -- see [Network failure semantics](#95-network-failure-semantics) for the full matrix and [`apm.yml` policy block](../../reference/manifest-schema/#39-policy) for the consumer-side fields.

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

:::note[Discovery vs. `extends:` -- two different concepts]
APM auto-discovers exactly **one** policy file: `<org>/.github/apm-policy.yml`, derived from the project's git remote. There is no automatic per-repo or per-enterprise discovery. `extends:` is what composes policies **inside** that one discovered file -- it lets the discovered policy pull in a parent (and that parent's parent, up to `MAX_CHAIN_DEPTH=5`) so you can model an enterprise -> org -> team chain through composition. Most teams who say "3 levels (repo, org, enterprise)" actually want `extends:`, not more discovery sites.
:::

Policies can inherit from a parent using `extends`. This enables a three-level chain:

```
Enterprise hub -> Org policy -> Repo override
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

---

## Install-time enforcement

:::note[Non-goal: structured output]
Install-time enforcement does **NOT** emit JSON or SARIF. The output is human-readable terminal text only. For machine-readable policy reports (CI gating, dashboards, code-scanning uploads) use `apm audit --ci --format json` or `apm audit --ci --format sarif` — see [`apm audit`](../../reference/cli-commands/#apm-audit---scan-for-hidden-unicode-characters) in the CLI reference.
:::

### 1. What APM policy is

`apm-policy.yml` is the contract an organization publishes to govern which packages, MCP servers, compilation targets, and manifest shapes its repositories may use. The schema is documented above; this section covers how that contract is enforced at `apm install` time.

### 2. Discovery and applicability

APM auto-discovers policy from `<org>/.github/apm-policy.yml` for any GitHub remote — both `github.com` and GitHub Enterprise (GHE). Repositories on non-GitHub remotes (ADO, GitLab, plain git) currently fall through with no policy applied; this is tracked as a follow-up. Repositories with no detectable git remote (unpacked bundles, temp directories) emit an explicit "could not determine org" line and skip discovery.

The `--policy <override>` flag is **audit-only today** — it works on `apm audit --ci` but is not yet wired through `apm install`. Use the escape hatches in section 8 if you need to bypass install-time enforcement for a single invocation.

### 3. Inheritance and composition

Policy resolves through the chain documented in [Inheritance](#inheritance) above: enterprise hub -> org -> repo override. The merge is **tighten-only**: a child can narrow allow lists, add deny entries, and escalate enforcement, but never relax a parent constraint. The full merge rule table is in [Tighten-only merge rules](#tighten-only-merge-rules).

Install-time enforcement and `apm audit --ci` both resolve the **full multi-level `extends:` chain** (enterprise hub -> org -> repo, or any depth up to `MAX_CHAIN_DEPTH = 5`). The walker fetches each parent via the same single-policy fetcher used for direct discovery, so caching, retries, and source-prefix handling are consistent across levels. Cycles (`A extends B`, `B extends A`) are detected by tracking visited refs and abort the walk with a clear error. If a parent fetch fails midway, APM merges the policies it already resolved and emits a `[!] Policy chain incomplete: <ref> unreachable, using <N> of <M> policies` warning so the operator learns that an upstream policy was unreachable.

### 4. What gets enforced

Install-time enforcement runs the same rule families documented in [Check reference](#check-reference):

- **Dependencies** — `allow`, `deny`, `require` (presence + optional version pin), `max_depth`.
- **MCP** — `allow`, `deny`, `transport.allow`, `self_defined`, `trust_transitive`.
- **Compilation** — `target.allow` / `target.enforce` (target-aware, evaluated against the resolved target list).
- **Manifest** — `required_fields`, `scripts`, `content_types.allow`.
- **Unmanaged files** — `action` against the configured `directories`.

### 5. When enforcement runs

| Command | Behaviour |
|---------|-----------|
| `apm install` | NEW — runs the policy gate after dependency resolution and before integration / target writes. Blocks before any files are deployed. |
| `apm install <pkg>` | NEW — snapshots `apm.yml`, runs the gate, rolls back the manifest on a block. |
| `apm install --mcp` | NEW — dedicated MCP preflight on the `--mcp` branch. |
| `apm deps update` | NEW — runs the install pipeline, so the same gate applies. |
| `apm install --dry-run` | NEW — read-only preflight; renders "would be blocked by policy" verdicts without mutating anything. |
| `apm audit --ci` | Existing — runs the same checks against the on-disk manifest + lockfile. |

`pack` and `bundle` are out of scope: they are author-side operations on packages being published, not consumers of dependencies.

### 6. Enforcement levels

`enforcement` is documented in [Top-level fields](#enforcement). The same three values (`off` / `warn` / `block`) apply at install time.

`require_resolution: project-wins` has a specific, narrow semantic that applies identically at install and audit time:

- It downgrades **version-pin mismatches** on required packages from a block to a warning. The repo's declared version is honoured.
- It does **NOT** downgrade missing required packages — those still block under `enforcement: block`.
- It does **NOT** override an inherited org `deny` — a parent's deny always wins over a child's allow or local declaration.

### 7. CLI examples

All examples below use the literal output APM emits today. Symbol legend: `[+]` success, `[!]` warning, `[x]` error, `[i]` info, `[*]` summary.

#### Successful install with policy resolved

`apm install` (verbose) against an org publishing `enforcement: block`, all dependencies allowed:

```shell
$ apm install --verbose
[i] Resolving dependencies...
[i] Policy: org:contoso/.github (cached, fetched 12m ago) -- enforcement=block
[+] Installed 4 APM dependencies, 2 MCP servers
```

Without `--verbose`, the `Policy:` line is suppressed for `enforcement=warn` and `enforcement=off`. Under `enforcement=block` it is **always** shown (rendered as a `[!]` warning) so users know blocking is active.

#### Block: denied dependency aborts the install

```shell
$ apm install
[i] Resolving dependencies...
[!] Policy: org:contoso/.github -- enforcement=block
[x] Policy violation: acme/evil-pkg -- Blocked by org policy at org:contoso/.github -- remove `acme/evil-pkg` from apm.yml, contact admin to update policy, or use `--no-policy` for one-off bypass
[x] Install aborted: 1 policy check failed
$ echo $?
1
```

The gate runs after dependency resolution and **before** any integrator writes files — `apm_modules/` and target configs are untouched.

#### Warn: denied dependency renders, install succeeds

Same denied dep, but the org policy ships `enforcement: warn`:

```shell
$ apm install
[i] Resolving dependencies...
[+] Installed 4 APM dependencies, 2 MCP servers

[!] Policy
    acme/evil-pkg -- Blocked by org policy at org:contoso/.github -- remove `acme/evil-pkg` from apm.yml, contact admin to update policy, or use `--no-policy` for one-off bypass
```

Violations flow through `DiagnosticCollector` and surface in the end-of-install summary under the `Policy` category. Exit code is `0`.

#### `--no-policy` flag: loud warning, install proceeds

```shell
$ apm install --no-policy
[!] Policy enforcement disabled by --no-policy for this invocation. This does NOT bypass apm audit --ci. CI will still fail the PR for the same policy violation.
[i] Resolving dependencies...
[+] Installed 4 APM dependencies, 2 MCP servers
```

#### `APM_POLICY_DISABLE=1` env var: identical wording

```shell
$ APM_POLICY_DISABLE=1 apm install
[!] Policy enforcement disabled by APM_POLICY_DISABLE=1 for this invocation. This does NOT bypass apm audit --ci. CI will still fail the PR for the same policy violation.
[i] Resolving dependencies...
[+] Installed 4 APM dependencies, 2 MCP servers
```

The warning is emitted on every invocation and cannot be silenced.

#### `--dry-run` with mixed allowed + denied + warn dependencies

Preview output is capped at five lines per severity bucket; overflow collapses into a single tail line:

```shell
$ apm install --dry-run
[i] Resolving dependencies...
[i] Policy: org:contoso/.github -- enforcement=block
[!] Would be blocked by policy: acme/evil-pkg -- denylist match: acme/evil-pkg
[!] Would be blocked by policy: acme/banned -- denylist match: acme/banned
[!] Would be blocked by policy: vendor/old -- denylist match: vendor/old
[!] Would be blocked by policy: vendor/legacy -- denylist match: vendor/legacy
[!] Would be blocked by policy: third/party -- denylist match: third/party
[!] ... and 2 more would be blocked by policy. Run `apm audit` for full report.
[!] Policy warning: contrib/optional -- required-package missing version pin
[i] Dry-run: no files written
```

#### `apm install <pkg>` blocked → manifest unchanged

`apm install <pkg>` mutates `apm.yml` before the pipeline runs. On a policy block, APM restores the manifest from a snapshot:

```shell
$ apm install acme/evil-pkg
[i] Resolving dependencies...
[!] Policy: org:contoso/.github -- enforcement=block
[x] Policy violation: acme/evil-pkg -- Blocked by org policy at org:contoso/.github -- remove `acme/evil-pkg` from apm.yml, contact admin to update policy, or use `--no-policy` for one-off bypass
[i] apm.yml restored to its previous state.
[x] Install aborted: 1 policy check failed
$ echo $?
1
```

#### Transitive MCP server blocked

When a dep brings in an MCP server denied by `mcp.deny` or rejected by `mcp.transport.allow`, APM packages still install but MCP configs are not written:

```shell
$ apm install
[i] Resolving dependencies...
[!] Policy: org:contoso/.github -- enforcement=block
[+] Installed 4 APM dependencies
[x] Transitive MCP server(s) blocked by org policy. APM packages remain installed; MCP configs were NOT written.

[!] Policy
    contrib/sketchy-mcp -- transport `http` not in mcp.transport.allow=[stdio]
$ echo $?
1
```

### 8. Escape hatches

**Non-bypass contract:** every escape hatch below is single-invocation, is not persisted to disk, and does **NOT** change CI behaviour. `apm audit --ci` will still fail the PR for the same policy violation. These hatches exist to unblock local debugging, not to circumvent governance.

| Hatch | Scope |
|-------|-------|
| `--no-policy` flag | Available on `apm install`, `apm install <pkg>`, and `apm install --mcp`. Skips discovery and enforcement for one invocation; emits a loud warning. Not currently exposed on `apm deps update`. |
| `APM_POLICY_DISABLE=1` env var | Equivalent to `--no-policy`. Same loud warning. |

`APM_POLICY` is reserved for a future override env var and is **not** equivalent to `APM_POLICY_DISABLE`.

### 9. Cache and offline behaviour

Resolved effective policy is cached under `apm_modules/.policy-cache/`. Default TTL is `cache.ttl` from the policy itself (`3600` seconds). Beyond TTL, APM will serve a stale cache on refresh failure with a loud warning, up to a hard ceiling of 7 days (`MAX_STALE_TTL`). `--no-cache` forces a fresh fetch and ignores any cached entry. Cache writes are atomic (temp file + rename) to survive concurrent installs.

### 9.5. Network failure semantics

When discovery cannot reach the policy source, APM behaves as follows:

- **Cached, stale within 7 days** — use the cached policy and emit a warning naming the cache age and the fetch error. Enforcement still applies.
- **Cache miss or stale beyond 7 days, fetch fails** — emit a loud warning every invocation; **do NOT block the install** by default (closes #829: ratified to keep developers unblocked when GitHub is unreachable). Opt in to fail-closed behaviour with `policy.fetch_failure: block` on the org policy (applies when a cached policy is available) or `policy.fetch_failure_default: block` in the project's `apm.yml` (applies when no policy is available at all). Both default to `warn`.
- **Garbage response** (HTTP 200 with non-YAML body, e.g. captive portal HTML) — same posture as fetch failure: warn loudly by default, block when the project pins `policy.fetch_failure_default: block`.

Example -- consumer-side opt-in to fail-closed semantics in `apm.yml`:

```yaml
name: my-project
version: '1.0'
policy:
  fetch_failure_default: block
```

### 9.6. Hash pin: `policy.hash` (consumer-side verification)

The org-side fetch_failure knob does not protect against a successful 200 OK response that happens to return *valid* YAML constructed by a compromised mirror, captive portal, or man-in-the-middle. To close that gap, projects can pin the exact bytes they expect to receive from the org policy source -- the `pip --require-hashes` equivalent for `apm-policy.yml`:

```yaml
name: my-project
version: '1.0'
policy:
  hash: "sha256:6a8c...e2f1"        # SHA-256 of the raw apm-policy.yml bytes
  hash_algorithm: sha256             # optional; sha256 (default), sha384, sha512
```

Compute the digest from the canonical org-policy file:

```bash
shasum -a 256 .github/apm-policy.yml | awk '{print "sha256:" $1}'
```

When set, every install / `apm policy status` / `apm audit --ci` verifies the hash of the fetched leaf policy bytes (UTF-8 encoded, **before** YAML parsing -- so re-serialized semantically-equivalent YAML still fails). A mismatch is **always** fail-closed regardless of `policy.fetch_failure` / `policy.fetch_failure_default`. The pin applies only to the leaf policy; parents in an `extends:` chain remain the leaf author's responsibility.

A malformed pin (unsupported algorithm, wrong length, non-hex) is rejected at parse time -- silently ignoring it would defeat the security guarantee. MD5 and SHA-1 are not accepted.

Compute the pin on Linux with `sha256sum .github/apm-policy.yml | awk '{print "sha256:" $1}'`.

### 9.7. `apm policy status`: diagnostic snapshot

Inspect the current policy posture without running an install or audit. The default exit code is always 0, so it is safe for human and SIEM use:

```shell
$ apm policy status
                APM Policy Status
+--------------------+-----------------------------------+
| Field              | Value                             |
+--------------------+-----------------------------------+
| Outcome            | found                             |
| Source             | org:contoso/.github               |
| Enforcement        | block                             |
| Cache age          | 12m ago                           |
| Extends chain      | none                              |
| Effective rules    | 3 dependency denies; 2 mcp denies |
+--------------------+-----------------------------------+
```

JSON output for CI / scripting:

```shell
$ apm policy status --json
{
  "outcome": "found",
  "source": "org:contoso/.github",
  "enforcement": "block",
  "cache_age_seconds": 720,
  "extends_chain": [],
  "rule_counts": { ... },
  "rule_summary": ["3 dependency denies", "2 mcp denies"]
}
```

Flags:

- `--policy-source <ref>` overrides discovery (path, `owner/repo`, `https://...`, or `org`).
- `--no-cache` forces a fresh fetch.
- `--json` / `-o json` switches to JSON output.
- `--check` exits non-zero (1) when no usable policy is found (anything other than `outcome=found`). Use this for CI pre-checks that must fail when org policy is unreachable or misconfigured. Default behaviour (without `--check`) remains exit-0.

```shell
$ apm policy status --check          # exits 1 if outcome != "found"
$ apm policy status --check --json   # exit 1 + JSON body for CI tooling
```

### 9.8. `apm audit --ci` auto-discovery

When `--policy` (alias `--policy-source`) is omitted, `apm audit --ci` mirrors the install-time discovery path: it auto-discovers the org policy from the git remote, applying the same checks CI runs in production. Add `--no-policy` to skip discovery for a single invocation:

```shell
$ apm audit --ci                     # auto-discovers org policy
$ apm audit --ci --policy <local>    # explicit override
$ apm audit --ci --no-policy         # baseline checks only
```

### 10. Error and exit-code reference

#### Discovery outcomes

Each row maps a `PolicyFetchResult.outcome` to its exit impact, severity, the message APM emits, and the recommended fix.

| Outcome | Exit | Severity | Primary message | Remediation |
|---------|------|----------|-----------------|-------------|
| `found` | `0` (or `1` if checks fail under `block`) | info / block | `Policy: <source> (cached, fetched Nm ago) -- enforcement=<level>` | None; enforcement applied. Under `block`, fix violations or use `--no-policy` for one-off bypass. |
| `absent` | `0` | info | `No org policy found for <host_org>` | None required. To publish one, see section 11. |
| `cached_stale` | `0` (enforcement still applies) | warn | `Policy: <source> (cached, fetched Nm ago) -- enforcement=<level>` plus refresh-error warning | Restore network reachability or run with `--no-cache` once connectivity returns. |
| `cache_miss_fetch_fail` | `0` | warn | `Could not fetch org policy (<error>) -- policy enforcement skipped for this invocation` | Retry, check VPN/firewall/`gh auth status`/`GITHUB_APM_PAT`. Fail-open by design (CEO-ratified); CI will still fail for the same violation. |
| `garbage_response` | `0` | warn | `Could not fetch org policy (invalid YAML body from <source>) -- policy enforcement skipped for this invocation` | Likely a captive portal or auth wall returning HTML. Restore direct connectivity, then re-run. |
| `malformed` | `0` (no enforcement) | warn | `Policy at <source> is malformed -- contact your org admin to fix the policy file` | Contact org admin to fix the YAML. Validate locally with `apm audit --ci --policy <local-path>`. |
| `manifest-parse` | `1` (always) | error | `Cannot parse apm.yml: <error>` | Fix the YAML syntax error in `apm.yml`. This is a local audit check (not a fetch outcome) -- malformed manifests always fail the audit unconditionally. |
| `disabled` | `0` | warn | `Policy enforcement disabled by --no-policy for this invocation. This does NOT bypass apm audit --ci. CI will still fail the PR for the same policy violation.` | Single-invocation only. Drop the flag / env var to re-enable. |
| `no_git_remote` | `0` | warn | `Could not determine org from git remote; policy auto-discovery skipped` | Run inside a checkout with a GitHub remote, or set the remote with `git remote add origin <url>`. |
| `empty` | `0` | warn | `Org policy is present but empty; no enforcement applied` | Org admin should populate the policy file (see section 11) or remove it. |
| `hash_mismatch` | `1` (always) | error | `Policy hash mismatch: pinned hash does not match fetched policy. Update apm.yml policy.hash or contact your org admin.` | Inspect the diff between expected and actual digest in the error output. If the org legitimately rotated the policy, recompute and update `policy.hash` in `apm.yml`. Otherwise, treat as a potential supply-chain compromise and contact your org admin. |

#### Violation classes

When `enforcement=block`, any of the following exit `1` and abort before integration. When `enforcement=warn`, they render in the post-install summary under the `Policy` category and exit `0`.

| Class | Origin | Primary message | Remediation |
|-------|--------|-----------------|-------------|
| `denylist` | `dependencies.deny` match | `Policy violation: <dep> -- Blocked by org policy at <source> -- remove <dep> from apm.yml, contact admin to update policy, or use --no-policy for one-off bypass` | Remove the dep from `apm.yml`, request an org-policy update, or `--no-policy` for one-off local debugging. |
| `allowlist` | Dep not in non-empty `dependencies.allow` | `Policy violation: <dep> -- not in dependencies.allow` | Add the dep to the org allowlist or switch to an approved package. |
| `required` | Missing `dependencies.require` entry, or pin mismatch | `Policy violation: <required-dep> -- required by org policy but not declared in apm.yml` (or `... required >=X but apm.yml pins <Y>`) | Add the required dep to `apm.yml` (and pin the required version). Pin mismatches downgrade to warn under `require_resolution: project-wins`; missing required deps still block. |
| `transport` | MCP transport not in `mcp.transport.allow` | `Policy violation: <mcp-server> -- transport <t> not in mcp.transport.allow=[<list>]` | Switch the server to an allowed transport, or request `mcp.transport.allow` updates. |
| `target` | Resolved target not in `compilation.target.allow` (or violates `target.enforce`) | `Policy violation: target <t> -- not in compilation.target.allow=[<list>]` | Re-run with `--target <allowed>`, or update `compilation.target` in `apm.yml`. Evaluated post-`targets` phase, so CLI overrides are honoured. |
| `transitive_mcp` | MCP server pulled in by a transitive dep, blocked by `mcp.deny`/`transport`/`self_defined` | `Transitive MCP server(s) blocked by org policy. APM packages remain installed; MCP configs were NOT written.` plus per-server `Policy violation: ...` | Remove the offending dep, request an org policy update, or set `mcp.trust_transitive: true` if the org chooses to allow transitive MCP entries. |

All violation messages above flow through `InstallLogger.policy_violation`; under `block` they print inline as `[x]` errors and exit `1`. Use `apm audit --ci --format json` for the same set of findings in machine-readable form.

### 11. For org admins

Checklist to publish a policy:

1. Create `<org>/.github/apm-policy.yml` in the org's `.github` repository.
2. Start from the [Standard org policy](#standard-org-policy) example above and trim it to the minimum that reflects your governance posture.
3. Set `enforcement: warn` first. Let CI surface diagnostics across consuming repos for one cycle without breaking installs.
4. When the warn-cycle is clean, switch to `enforcement: block`. Communicate the change in your org's CHANGELOG/announcements channel — `apm install` will start failing for any non-compliant repo.
5. Use `extends:` to layer team-specific policies on top of the org baseline rather than forking the file.

Recommended starter:

```yaml
name: "<Org> APM Policy"
version: "0.1.0"
enforcement: warn

dependencies:
  allow:
    - "<org>/**"
  max_depth: 5

mcp:
  self_defined: warn

manifest:
  required_fields: [version, description]
```

---

## Related

- [Governance](../../enterprise/governance-guide/) -- conceptual overview, bypass contract, and rollout playbook
- [CI Policy Enforcement](../../guides/ci-policy-setup/) -- step-by-step CI setup tutorial
- [GitHub Rulesets](../../integrations/github-rulesets/) -- enforce policy as a required status check
