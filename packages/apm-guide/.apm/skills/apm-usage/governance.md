# Governance and Policy

**Note:** The policy engine is experimental (early preview). Schema fields and
defaults may change between releases. Pin your APM version and monitor the
CHANGELOG when using policy features.

## Policy file location

- **Org-level:** hosted in a repo, fetched via `--policy org` or `--policy URL`
- **Repo-level:** `apm-policy.yml` in the repository root
- **Local override:** `--policy ./path/to/apm-policy.yml`

## Policy schema overview

```yaml
name: "Contoso Engineering Policy"
version: "1.0.0"
extends: org                             # inherit from parent policy
enforcement: block                       # off | warn | block
fetch_failure: warn                      # warn | block; org-side knob (see 9.5)

cache:
  ttl: 3600                             # policy cache in seconds

dependencies:
  allow: []                             # allowed patterns
  deny: []                              # denied patterns (takes precedence)
  require: []                           # required packages
  require_resolution: project-wins      # project-wins | policy-wins | block
  max_depth: 50                         # transitive depth limit

mcp:
  allow: []                             # allowed server patterns
  deny: []                              # denied patterns
  transport:
    allow: []                           # stdio | sse | http | streamable-http
  self_defined: warn                    # deny | warn | allow
  trust_transitive: false               # trust MCP from transitive deps

compilation:
  target:
    allow: [vscode, claude]             # permitted targets
    enforce: null                       # force specific target (must be present in target list)
  strategy:
    enforce: null                       # distributed | single-file
  source_attribution: false             # require attribution

manifest:
  required_fields: []                   # fields that must exist in apm.yml
  scripts: allow                        # allow | deny
  content_types:
    allow: []                           # instructions | skill | hybrid | prompts
  require_explicit_includes: false      # mandate explicit `includes:` list in apm.yml (rejects `auto` and undeclared)

unmanaged_files:
  action: ignore                        # ignore | warn | deny
  directories: []                       # directories to scan
```

## Local content governance

The `includes:` field in `apm.yml` controls which local `.apm/` content the
package publishes:

- `includes: auto` -- publish all local `.apm/` content (default, convenient).
- `includes: [path/to/file, ...]` -- explicit list of paths (governance-friendly).

For compliance, prefer the explicit list and pair it with
`policy.manifest.require_explicit_includes: true`, which rejects `auto` and
undeclared local content at install / audit time.

## Enforcement modes

| Value | Behavior |
|-------|----------|
| `off` | Checks skipped entirely |
| `warn` | Violations reported but do not fail |
| `block` | Violations abort `apm install` (exit 1) AND fail `apm audit --ci` |

## Inheritance rules (tighten-only)

Child policies can only tighten parent policies, never relax them:

| Field | Merge rule |
|-------|-----------|
| `enforcement` | Escalates: `off` < `warn` < `block` |
| Allow lists | Intersection (child narrows parent) |
| Deny lists | Union (child adds to parent) |
| `require` | Union (combines required packages) |
| `max_depth` | `min(parent, child)` |
| `mcp.self_defined` | Escalates: `allow` < `warn` < `deny` |
| `source_attribution` | `parent OR child` (either enables) |

Chain limit: 5 levels max. Cycles are detected and rejected.

## Pattern matching syntax

| Pattern | Matches |
|---------|---------|
| `contoso/*` | `contoso/repo` (single segment only) |
| `contoso/**` | `contoso/repo`, `contoso/org/repo`, any depth |
| `*/approved` | `any-org/approved` |
| `exact/match` | Only `exact/match` |

Deny is evaluated first. Empty allow list permits all (except denied).

## Baseline checks (always run with --ci)

These checks run without a policy file:

- `lockfile-exists` -- apm.lock.yaml present
- `ref-consistency` -- dependency refs match lockfile
- `deployed-files-present` -- all deployed files exist
- `no-orphaned-packages` -- no packages in lockfile absent from manifest
- `config-consistency` -- MCP configs match lockfile
- `content-integrity` -- no critical Unicode in deployed files

## Policy checks (with --policy)

Additional checks when a policy is provided:

- **Dependencies:** allowlist, denylist, required packages, transitive depth
- **MCP:** allowlist, denylist, transport, self-defined servers
- **Compilation:** target, strategy, source attribution
- **Manifest:** required fields, scripts policy
- **Unmanaged:** unmanaged file detection

## CLI usage

```bash
apm audit --ci                              # baseline checks only
apm audit --ci --policy org                 # auto-discover org policy
apm audit --ci --policy ./apm-policy.yml    # local policy file
apm audit --ci --policy https://...         # remote policy URL
```

## Install-time enforcement

**Note:** Install-time policy enforcement (issue #827) is in active development. The behaviour described below reflects the shipping design.

**Non-goal — structured output:** install-time enforcement does NOT emit JSON or SARIF. Output is human-readable terminal text only. For machine-readable policy reports use `apm audit --ci --format json` or `apm audit --ci --format sarif`.

### 1. What APM policy is

`apm-policy.yml` is the contract an organization publishes to govern which
packages, MCP servers, compilation targets, and manifest shapes its repositories
may use. This section covers how that contract is enforced at `apm install` time.

### 2. Discovery and applicability

APM auto-discovers policy from `<org>/.github/apm-policy.yml` for any GitHub
remote — both `github.com` and GitHub Enterprise (GHE). Non-GitHub remotes (ADO,
GitLab, plain git) currently fall through with no policy applied; tracked as a
follow-up. Repositories with no detectable git remote (unpacked bundles, temp
dirs) emit an explicit "could not determine org" line and skip discovery.

The `--policy <override>` flag is **audit-only today** — it works on
`apm audit --ci` but is not yet wired through `apm install`.

### 3. Inheritance and composition

Policy resolves through the chain: enterprise hub -> org -> repo override.
The merge is **tighten-only** (see "Inheritance rules" above).

**Multi-level extends:** install-time enforcement and `apm audit --ci` both
resolve the full `extends:` chain up to `MAX_CHAIN_DEPTH = 5`. Cycles are
detected and abort with an error. If a parent fetch fails midway, APM
merges what it resolved and emits a `Policy chain incomplete` warning.

### 4. What gets enforced

- **Dependencies:** allow, deny, require (presence + optional version pin), max_depth
- **MCP:** allow, deny, transport.allow, self_defined, trust_transitive
- **Compilation:** target.allow / target.enforce (target-aware)
- **Manifest:** required_fields, scripts, content_types.allow
- **Unmanaged files:** action against configured directories

### 5. When enforcement runs

| Command | Behaviour |
|---------|-----------|
| `apm install` | NEW — gate runs after resolve, before integration / target writes |
| `apm install <pkg>` | NEW — snapshot apm.yml, run gate, rollback on block |
| `apm install --mcp` | NEW — dedicated MCP preflight |
| `apm deps update` | NEW — runs the install pipeline, so the same gate applies |
| `apm install --dry-run` | NEW — read-only preflight; renders "would be blocked" |
| `apm audit --ci` | Existing — same checks against on-disk manifest + lockfile |

`pack` and `bundle` are out of scope (author-side, not dependency consumers).

### 6. Enforcement levels

`off` / `warn` / `block` apply identically at install and audit time.
`require_resolution: project-wins` has a narrow semantic:

- Downgrades **version-pin mismatches** on required packages to warnings only.
- Does **NOT** downgrade missing required packages — those still block under
  `enforcement: block`.
- Does **NOT** override an inherited org `deny` — parent deny always wins.

### 7. CLI examples

Symbol legend: `[+]` success, `[!]` warning, `[x]` error, `[i]` info.

Successful install (verbose) under `enforcement: block`:

```shell
$ apm install --verbose
[i] Resolving dependencies...
[i] Policy: org:contoso/.github (cached, fetched 12m ago) -- enforcement=block
[+] Installed 4 APM dependencies, 2 MCP servers
```

Block: denied dependency aborts the install before integration:

```shell
$ apm install
[i] Resolving dependencies...
[!] Policy: org:contoso/.github -- enforcement=block
[x] Policy violation: acme/evil-pkg -- Blocked by org policy at org:contoso/.github -- remove `acme/evil-pkg` from apm.yml, contact admin to update policy, or use `--no-policy` for one-off bypass
[x] Install aborted: 1 policy check failed
```

Warn: same dep, `enforcement: warn` -- install succeeds, violation flows to summary:

```shell
$ apm install
[i] Resolving dependencies...
[+] Installed 4 APM dependencies, 2 MCP servers

[!] Policy
    acme/evil-pkg -- Blocked by org policy at org:contoso/.github -- remove `acme/evil-pkg` from apm.yml, contact admin to update policy, or use `--no-policy` for one-off bypass
```

Escape hatches (`--no-policy` flag and `APM_POLICY_DISABLE=1` env var) emit the same loud warning every invocation:

```shell
$ apm install --no-policy
[!] Policy enforcement disabled by --no-policy for this invocation. This does NOT bypass apm audit --ci. CI will still fail the PR for the same policy violation.
[i] Resolving dependencies...
[+] Installed 4 APM dependencies, 2 MCP servers
```

`--dry-run` previews violations (capped at five per severity bucket; overflow collapses):

```shell
$ apm install --dry-run
[i] Resolving dependencies...
[i] Policy: org:contoso/.github -- enforcement=block
[!] Would be blocked by policy: acme/evil-pkg -- denylist match: acme/evil-pkg
[!] Would be blocked by policy: acme/banned -- denylist match: acme/banned
[!] ... and 4 more would be blocked by policy. Run `apm audit` for full report.
[i] Dry-run: no files written
```

`apm install <pkg>` blocked -- manifest restored:

```shell
$ apm install acme/evil-pkg
[i] Resolving dependencies...
[!] Policy: org:contoso/.github -- enforcement=block
[x] Policy violation: acme/evil-pkg -- Blocked by org policy at org:contoso/.github -- remove `acme/evil-pkg` from apm.yml, contact admin to update policy, or use `--no-policy` for one-off bypass
[i] apm.yml restored to its previous state.
[x] Install aborted: 1 policy check failed
```

Transitive MCP server blocked -- APM packages stay installed, MCP configs are not written:

```shell
$ apm install
[i] Resolving dependencies...
[!] Policy: org:contoso/.github -- enforcement=block
[+] Installed 4 APM dependencies
[x] Transitive MCP server(s) blocked by org policy. APM packages remain installed; MCP configs were NOT written.
```

### 8. Escape hatches

**Non-bypass contract:** every hatch below is single-invocation, is not
persisted, and does **NOT** change CI behaviour. `apm audit --ci` will still
fail the PR for the same policy violation.

| Hatch | Scope |
|-------|-------|
| `--no-policy` | On `apm install`, `apm install <pkg>`, `apm install --mcp`. Skips discovery + enforcement; loud warning. Not on `apm deps update`. |
| `APM_POLICY_DISABLE=1` | Env var equivalent. Same loud warning. |

`APM_POLICY` is reserved for a future override env var and is **not**
equivalent to `APM_POLICY_DISABLE`.

### 9. Cache and offline behaviour

Resolved effective policy is cached under `apm_modules/.policy-cache/`. Default
TTL comes from the policy's `cache.ttl` (`3600` seconds). Beyond TTL, APM serves
the stale cache on refresh failure with a loud warning, up to a hard ceiling
of 7 days (`MAX_STALE_TTL`). `--no-cache` forces a fresh fetch. Writes are
atomic (temp file + rename).

### 9.5. Network failure semantics

- **Cached, stale within 7 days:** use cache + warn naming age and error.
  Enforcement still applies.
- **Cache miss or stale beyond 7 days, fetch fails:** loud warning every
  invocation; **do NOT block the install** by default (closes #829).
- **Garbage response** (HTTP 200 with non-YAML body, e.g. captive portal):
  same posture as fetch failure -- warn loudly, cache fallback if present.

Opt in to fail-closed semantics with the `policy.fetch_failure: warn|block`
knob on `apm-policy.yml` (applies when a cached policy is available) or
`policy.fetch_failure_default: warn|block` in the project's `apm.yml`
(applies when no policy is available at all). Both default to `warn`.

### 9.6. Hash pin (`policy.hash`)

Consumer-side bytes-pin in `apm.yml` -- the `pip --require-hashes`
equivalent for `apm-policy.yml`. Closes the compromised-mirror /
captive-portal vector where a 200 OK with valid-looking but tampered YAML
would otherwise install.

```yaml
policy:
  hash: "sha256:<hex>"
  hash_algorithm: sha256   # optional; sha256 (default), sha384, sha512
```

Hash is computed on the raw UTF-8 bytes of the leaf policy (before YAML
parsing). A mismatch is **always** fail-closed regardless of
`policy.fetch_failure*` settings. Malformed pins are rejected at parse
time. MD5 / SHA-1 not accepted.

### 9.7. Diagnostic command

`apm policy status` prints discovery outcome, source, enforcement, cache
age, `extends` chain, and rule counts (table or `--json`). Always exits 0
so it is safe for CI / SIEM ingestion. Supports `--policy-source` and
`--no-cache`.

### 9.8. `apm audit --ci` auto-discovery

When `--policy` (alias `--policy-source`) is omitted, `apm audit --ci`
auto-discovers the org policy from the git remote, mirroring the install
path. Use `--no-policy` to skip discovery for a single invocation.

### 10. Errors and exit codes

All discovery outcomes exit `0` except `found` under `enforcement: block`
with at least one violation (exit `1`) and `hash_mismatch` (always exit
`1`).

Discovery outcomes APM can emit (see `PolicyFetchResult.outcome`):
`found`, `absent`, `cached_stale`, `cache_miss_fetch_fail`, `garbage_response`,
`malformed`, `disabled`, `no_git_remote`, `empty`, `hash_mismatch`.
`hash_mismatch` is always fail-closed; the other fetch-failure outcomes
are fail-open by default and become fail-closed when the project opts in
via `policy.fetch_failure_default: block`.

A malformed project manifest (`apm.yml`) is a separate concern from a
malformed policy file. When `apm.yml` cannot be parsed (invalid YAML or
non-mapping content), both `run_policy_checks()` and
`run_baseline_checks()` produce a failing `manifest-parse` check. This
is unconditionally fail-closed and cannot be relaxed.

Violation classes:

| Class | Triggers | Remediation |
|-------|----------|-------------|
| `denylist` | `dependencies.deny` match | Remove dep from `apm.yml`, request org-policy update, or `--no-policy` for one-off bypass |
| `allowlist` | Dep not in non-empty `dependencies.allow` | Add to org allowlist or switch to an approved package |
| `required` | Missing `dependencies.require` entry, or version-pin mismatch | Add the dep (and pin) to `apm.yml`. Pin mismatches downgrade to warn under `require_resolution: project-wins`; missing required deps still block |
| `transport` | MCP transport not in `mcp.transport.allow` | Switch transport, or request `mcp.transport.allow` update |
| `target` | Resolved target not in `compilation.target.allow` (or violates `target.enforce`) | Re-run with `--target <allowed>`, or adjust `compilation.target` in `apm.yml` |
| `transitive_mcp` | MCP server pulled in by a transitive dep, blocked by `mcp.deny` / `transport` / `self_defined` | Remove offending dep, request policy update, or set `mcp.trust_transitive: true` |

Full message text per outcome and per class lives in
`docs/src/content/docs/enterprise/policy-reference.md` §10. Violation messages
flow through `InstallLogger.policy_violation`; under `block` they print inline
as `[x]` errors and exit `1`.

### 11. For org admins

Checklist to publish a policy:

1. Create `<org>/.github/apm-policy.yml` in the org's `.github` repository.
2. Start from the recommended starter below and trim to the minimum reflecting
   your governance posture.
3. Set `enforcement: warn` first. Let CI surface diagnostics across consuming
   repos for one cycle without breaking installs.
4. When the warn-cycle is clean, switch to `enforcement: block`. Communicate
   the change — `apm install` will start failing for non-compliant repos.
5. Use `extends:` for team-specific overrides on top of the org baseline
   rather than forking the file.

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
