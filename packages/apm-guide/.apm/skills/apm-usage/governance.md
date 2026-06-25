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
  require_pinned_constraint: false      # when true, ban unbounded dep ranges (NO_REF, '*', bare branch, '>=X' without upper bound)

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
  exclude: []                           # path globs to suppress (known harness-managed files)

registry_source:                        # experimental: requires `apm experimental enable registries`
  require: []                           # registry names that MUST be reachable in the merged registry map
  allow_non_registry: true              # when false, blocks any dep not routed through a configured registry

bin_deploy:                             # DEPRECATED alias, folded into executables.deny (bin type)
  deny_all: false                       # when true, suppress bin/ deploy for every plugin
  deny: []                              # canonical strings (owner/name) whose bin/ must not deploy

executables:                            # org ceiling for executable-primitive trust (issue #1873)
  deny_all: false                       # when true, block EVERY executable type for all packages
  deny: []                              # packages whose executables must not deploy (deny is the ceiling)
  require: []                           # packages whose executables MUST be present and trusted
  recommend: []                         # org-vetted set; default-allow unless locally denied
```

## Registry source governance (experimental)

Gate dependency sources to REST-based APM registries declared via the
`registries:` block in `apm.yml` (or in `~/.apm/config.json`). Applies
to direct AND transitive dependencies.

```yaml
# .github/apm-policy.yml
registry_source:
  require:
    - corp-main                         # this registry MUST be reachable
  allow_non_registry: false             # block any dep not routed through a registry
```

| Field | Default | Behavior |
|-------|---------|----------|
| `require` | `[]` | Registry names that MUST appear in the merged registry map (project `apm.yml` + workspace `~/.apm/apm.yml` + `~/.apm/config.json`). Fail-closed if a listed name has no URL. |
| `allow_non_registry` | `true` | When `false`, every dep MUST be routed through a configured registry; git-shorthand and `- git:` deps are blocked at install time. |

The check fires from all four call sites (`policy_gate`,
`policy_target_check`, `run_policy_checks`, `run_policy_preflight`) so
`apm install`, `apm install <pkg>`, `apm deps update`, and
`apm audit --ci` all enforce the same gate.

## Integrity and drift enforcement

Two additive, optional, default-off keys under the existing `security:`
namespace, both backed by enforcement that exists today.

```yaml
# .github/apm-policy.yml
security:
  integrity:
    require_hashes: true    # fail install closed if any locked entry lacks a hash
  audit:
    fail_on_drift: true     # `apm audit` exits non-zero on workspace drift
```

| Field | Default | Behavior |
|-------|---------|----------|
| `integrity.require_hashes` | `false` | When `true`, every non-local lockfile entry MUST carry a content hash; a missing or empty hash fails `apm install` closed. Asserts hash-presence on the freshly-built lockfile (no second hashing pass). Local deps are exempt. Logical OR on inheritance. |
| `audit.fail_on_drift` | `false` | When `true`, a bare `apm audit` exits non-zero when workspace content drifts from the lockfile (default-off keeps drift advisory at exit 0). Only changes the exit code; `apm audit --ci` already gates on drift. Logical OR on inheritance. |

## External scanner governance (experimental)

Gate the behaviour of third-party SARIF scanners run by `apm audit
--external <name>` (behind `apm experimental enable external-scanners`).
The stance is **restrict-only**: policy can tighten scanner behaviour but
never adds argv tokens itself and never forces LLM egress from an
untrusted project-local policy.

```yaml
# .github/apm-policy.yml
security:
  audit:
    external: [skillspector]              # scanners the org permits
    scanners:                             # NEW, optional per-scanner governance
      skillspector:
        allow_args: false                 # strip all user/CLI extra-args (kill-switch)
```

| Field | Default | Behavior |
|-------|---------|----------|
| `scanners.<name>.allow_args` | unset (no opinion) | When `false`, all user/CLI `--external-args` and config `external.<name>.args` are stripped to an empty list before the scanner runs -- locks the scanner to its vetted invocation. AND-merged across inheritance: any ancestor setting `false` wins. |

Notes:
- Policy **never injects argv** -- only the local user contributes scanner
  flags (via `--external-args` or `external.<name>.args`), and those are
  allowlist-validated by the adapter.
- `allow_args: false` is enforced at the **install-time** audit path (which
  loads org policy). A bare `apm audit` does not load org policy, so it
  relies on the adapter's allowlist for arg safety.
- LLM mode is opt-in by the user only; a project-local policy cannot mandate
  it (this avoids turning a checked-in policy file into a content-exfiltration
  channel).

## Executable trust governance

Issue #1873 unifies executable-primitive trust (hooks, `bin/` executables,
self-defined MCP servers, canvas extensions) onto one noun, `executables`,
across three layers. The org policy is the **ceiling on deny**: it can deny and
require fleet-wide and recommend a vetted set, but personal or project consent
can never widen past an org deny.

```yaml
# .github/apm-policy.yml
executables:
  deny_all: false                       # kill-switch: deny every executable type org-wide
  deny: ["evil/*"]                      # packages whose executables must not deploy (ceiling)
  require: ["acme/ci"]                  # executables MUST be present and trusted
  recommend: ["acme/fmt"]               # org-vetted; default-allow unless locally denied
```

| Field | Default | Behavior |
|-------|---------|----------|
| `deny_all` | `false` | When `true`, blocks every executable type for every package. |
| `deny` | `[]` | Canonical package strings whose executables must not deploy. **Deny always wins** and is the only side that supports `fnmatch` globs in v1 (e.g. `evil/*` blocks every package under `evil/`). Union-merged across inheritance. |
| `require` | `[]` | Packages whose executables MUST be present and trusted (exact-match in v1). Union-merged. `require` mandates presence + trust but does **not** grant execution -- it stays a developer-consent decision. To mandate AND auto-deploy fleet-wide, list the package in BOTH `require` and `recommend`. |
| `recommend` | `[]` | Org-vetted set (exact-match in v1); default-allowed unless locally denied. Bulk-accepted with `apm approve --recommended`. Union-merged. |
| `enforce` | `[]` | v2 mandate tier; **accepted but INERT in v1** -- degrades to `recommend` (no force-execute; a user deny still overrides). Writing it emits a deprecation-style warning. |

Glob scope (v1): only `deny` supports glob patterns (the safety ceiling). `allow`, `recommend`, and `require` are exact-match only -- widening the GRANT side with a wildcard has a larger blast radius and is deferred.

The install gate and `apm audit` resolve trust through one shared deny-wins,
first-match-wins ladder:

```
1. org deny_all / org deny   -> denied (absolute ceiling)
2. user deny                 -> denied
3. project deny              -> denied
4. project allow             -> allowed
5. user allow                -> allowed
6. org recommend             -> allowed (user-overridable)
7. (no match)                -> gated pending approval (denied but approvable)
```

A package listed only in org `enforce` (the v2 mandate tier) collapses into
rung 6: it resolves as allowed-but-user-overridable, and `apm policy explain`
labels its deciding layer `org-enforce-degraded` to make the v1 degrade explicit.

The project layer is `apm.yml` `executables.{allow,deny}` (committed, via
`apm approve` / `apm deny`); the user layer is `~/.apm/config.json`
`executables.{allow,deny}` (machine-local, via `--user`, lowest authority).
Each locked dependency records its resolved state in the `exec_status` field of
`apm.lock.yaml` (`deployed`, `gated_pending_approval`, `denied`, `absent`).

In CI, `apm install` SUCCEEDS when a required package is present-but-parked
(executables gated pending approval) and prints a one-command remedy
(`apm approve <pkg>`); the `required-packages-deployed` audit check asserts
package PRESENCE, not materialized files. A separate audit signal,
`required-executable-untrusted`, hard-fails CI when a required package's
executables are untrusted (denied or gated).

There is no `enforce` mandate runtime, no cryptographic signing, and no
content-hash binding in this release: an org `executables.enforce` rung is
accepted but fail-safe degrades to `recommend` (allowed, still overridable by a
deny). Inspect the deciding layer for one package with `apm policy explain
<pkg>`, and surface fleet-wide layer conflicts (packages allowed locally but
denied by org policy) with `apm doctor`.

## Plugin bin/ deployment governance (deprecated alias)

> `bin_deploy` is the bin-scoped predecessor of `executables`. It is folded into
> `executables.deny` (bin type only) and honored as an alias for one minor
> cycle. Prefer `executables.deny` for new policies.

When a `marketplace_plugin` package ships a `bin/` directory, a global
install (`apm install -g`) deploys those executables into
`~/.claude/skills/<name>/bin/` so Claude Code invokes them as bare
commands (the skills-directory plugin contract). Deployment is
Claude-only and user-scope only; project-scope installs never deploy
executables.

```yaml
# .github/apm-policy.yml
bin_deploy:
  deny_all: true                        # block every plugin's bin/ deploy org-wide
  # or target specific packages:
  deny:
    - myorg/untrusted-plugin            # canonical owner/name string
```

| Field | Default | Behavior |
|-------|---------|----------|
| `deny_all` | `false` | When `true`, suppresses bin/ deployment for every `marketplace_plugin`, regardless of `deny`. |
| `deny` | `[]` | Canonical package strings (`owner/name`) whose bin/ executables must not deploy. |

Deployed executables are placed on Claude Code's `PATH` and invoked
without further confirmation, so use this field to opt out in
environments where plugin executables are not trusted by default.

## Canvas extension trust (experimental)

Behind the `canvas` experimental flag, a package may ship a Copilot CLI canvas
extension under `.apm/extensions/<name>/extension.mjs` (executable Node.js).
Because a canvas from a dependency is arbitrary executable code, APM blocks
dependency-provided canvases when the project opts in to the executable gate:
the project must add an `executables:` block to `apm.yml` and run
`apm approve <pkg>` to deploy it. A first-party canvas in the root package being
installed deploys once the flag is on; dependency canvases always require
explicit approval.

By default `apm approve` records the grant in the project `apm.yml`
`executables.allow` block (committed, shared with the team); `apm approve --user`
records a personal grant in `~/.apm/config.json` (machine-local, never
committed). Adding an empty `executables: {}` enables the gate but grants trust
to nothing.

At **project scope** a canvas deploys to `.github/extensions/<name>/`. With
`--global`, a **dependency-provided** canvas deploys to
`~/.copilot/extensions/<name>/` so it is available in every Copilot session;
global install always requires executable-trust approval (full-account blast
radius), supports only the default `~/.copilot` location (a non-default
`$COPILOT_HOME` is refused), and does not deploy first-party root canvases
(package them as a dependency instead). `apm uninstall --global` prunes the
global canvas.

The trust gate is enforced on every install path -- normal install and offline
bundle install (`apm install <bundle>`) -- so a vendored bundle cannot smuggle
an executable canvas past trust. Canvas trust is unified with the `executables`
default-deny gate (hooks, bin, mcp, canvas); approve once and all four
executable types are governed consistently. The org `executables:` policy block
governs canvas trust alongside the other types (`deny_all`, `deny`, `require`,
`recommend`); a canvas-only policy knob is not part of this experimental
release.

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

## Inheritance rules

Most fields tighten as the policy chain descends. The exception is `deny` and
`require` lists: a child policy may use `[]` to explicitly clear an inherited
list (removing entries the parent set). All other fields obey the rules below:

| Field | Merge rule |
|-------|-----------|
| `enforcement` | Escalates: `off` < `warn` < `block` |
| Allow lists | Intersection (child narrows parent) |
| Deny lists | Union (child adds to parent). Omitting or `null` = transparent; `[]` = explicit empty override. |
| `require` | Union (combines required packages). Omitting or `null` = transparent; `[]` = explicit empty override. |
| `max_depth` | `min(parent, child)` |
| `require_pinned_constraint` | Logical OR (once enabled, child cannot relax) |
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

**Non-goal  --  structured output:** install-time enforcement does NOT emit JSON or SARIF. Output is human-readable terminal text only. For machine-readable policy reports use `apm audit --ci --format json` or `apm audit --ci --format sarif`.

### 1. What APM policy is

`apm-policy.yml` is the contract an organization publishes to govern which
packages, MCP servers, compilation targets, and manifest shapes its repositories
may use. This section covers how that contract is enforced at `apm install` time.

### 2. Discovery and applicability

APM auto-discovers org policy from the project's git remote by checking
`.github`, `.apm`, and `_apm` policy repos in order on GitHub API-compatible
hosts. Azure DevOps hosts use `_apm` only, because ADO rejects dot-prefixed
repository names. Repositories with no detectable git remote (unpacked bundles,
temp dirs) emit an explicit "could not determine org" line and skip discovery.

The `--policy <override>` flag is **audit-only today**  --  it works on
`apm audit --ci` but is not yet wired through `apm install`.

### 3. Inheritance and composition

Policy resolves through the chain: enterprise hub -> org -> repo override.
The merge follows "Inheritance rules" above (most fields tighten; deny/require lists support explicit `[]` override).

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
| `apm install` | NEW  --  gate runs after resolve, before integration / target writes |
| `apm install <pkg>` | NEW  --  snapshot apm.yml, run gate, rollback on block |
| `apm install --mcp` | NEW  --  dedicated MCP preflight |
| `apm deps update` | NEW  --  runs the install pipeline, so the same gate applies |
| `apm install --dry-run` | NEW  --  read-only preflight; renders "would be blocked" |
| `apm audit --ci` | Existing  --  same checks against on-disk manifest + lockfile |

`pack` and `bundle` are out of scope (author-side, not dependency consumers).

### 6. Enforcement levels

`off` / `warn` / `block` apply identically at install and audit time.
`require_resolution: project-wins` has a narrow semantic:

- Downgrades **version-pin mismatches** on required packages to warnings only.
- Does **NOT** downgrade missing required packages  --  those still block under
  `enforcement: block`.
- Does **NOT** override an inherited org `deny`  --  parent deny always wins.

### 7. CLI examples

Symbol legend: `[+]` success, `[!]` warning, `[x]` error, `[i]` info.

Successful install (verbose) under `enforcement: block`:

```shell
$ apm install --verbose
[i] Resolving dependencies...
[i] Policy: org:contoso/.github (cached, fetched 12m ago) -- enforcement=block
[+] Installed 4 APM dependencies, 2 MCP servers in 1.2s
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
[+] Installed 4 APM dependencies, 2 MCP servers in 1.2s

[!] Policy
    acme/evil-pkg -- Blocked by org policy at org:contoso/.github -- remove `acme/evil-pkg` from apm.yml, contact admin to update policy, or use `--no-policy` for one-off bypass
```

Escape hatches (`--no-policy` flag and `APM_POLICY_DISABLE=1` env var) emit the same loud warning every invocation:

```shell
$ apm install --no-policy
[!] Policy enforcement disabled by --no-policy for this invocation. This does NOT bypass apm audit --ci. CI will still fail the PR for the same policy violation.
[i] Resolving dependencies...
[+] Installed 4 APM dependencies, 2 MCP servers in 1.2s
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
[+] Installed 4 APM dependencies in 0.8s
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
- **No policy resolved (`no_git_remote` / `absent` / `empty`):** since
  #1159, these emit a `[!]` warning to stderr and honour
  `policy.fetch_failure_default: block` for parity with fetch failures.
  Pre-fix they were silently fail-open even with `block` set.

Opt in to fail-closed semantics with the `policy.fetch_failure: warn|block`
knob on `apm-policy.yml` (applies when a cached policy is available) or
`policy.fetch_failure_default: warn|block` in the project's `apm.yml`
(applies for fetch failures AND no-policy outcomes when no policy is
available at all). Both default to `warn`.

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

Since #1159, the no-policy outcomes (`no_git_remote`, `absent`, `empty`)
emit a `[!]` warning to stderr by default and exit 1 with `[x]` when
the project sets `policy.fetch_failure_default: block` -- pre-fix they
silently exited 0, leaving CI green with no enforcement applied. JSON
and SARIF output on stdout stays clean (all diagnostics on stderr).
Explicit `--policy <file>` keeps the legacy fall-through (no warning)
so opt-in pointers at minimal baseline files do not regress.

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
| `pinned-constraint` | `dependencies.require_pinned_constraint: true` + a direct dep with no ref, a wildcard, a bare branch, or a bare `>=X.Y` | Pin the dep to an exact version (`1.2.3` or npm/cargo-style `=1.2.3`; pip-style `==1.2.3` is not supported), caret/tilde/bounded semver range, literal `vX.Y.Z` tag, or a full SHA. Roll out enforcement with `warn` before `block`. |
| `transport` | MCP transport not in `mcp.transport.allow` | Switch transport, or request `mcp.transport.allow` update |
| `target` | Resolved target not in `compilation.target.allow` (or violates `target.enforce`) | Re-run with `--target <allowed>`, or adjust `compilation.target` in `apm.yml` |
| `transitive_mcp` | MCP server pulled in by a transitive dep, blocked by `mcp.deny` / `transport` / `self_defined` | Remove offending dep, request policy update, or set `mcp.trust_transitive: true` |

Full message text per outcome and per class lives in
`docs/src/content/docs/enterprise/policy-reference.md` section10. Violation messages
flow through `InstallLogger.policy_violation`; under `block` they print inline
as `[x]` errors and exit `1`.

### 11. For org admins

Checklist to publish a policy:

1. Create `apm-policy.yml` in the org policy repo (`.github` on GitHub, `_apm`
   project/repo on Azure DevOps).
2. Start from the recommended starter below and trim to the minimum reflecting
   your governance posture.
3. Set `enforcement: warn` first. Let CI surface diagnostics across consuming
   repos for one cycle without breaking installs.
4. When the warn-cycle is clean, switch to `enforcement: block`. Communicate
   the change  --  `apm install` will start failing for non-compliant repos.
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
