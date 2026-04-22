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

unmanaged_files:
  action: ignore                        # ignore | warn | deny
  directories: []                       # directories to scan
```

## Enforcement modes

| Value | Behavior |
|-------|----------|
| `off` | Checks skipped entirely |
| `warn` | Violations reported but do not fail |
| `block` | Violations cause `apm audit --ci` to exit 1 |

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

**Note:** Install-time policy enforcement (issue #827) is in active development.
The behaviour described below reflects the shipping design; `TODO` markers will
be filled with verbatim CLI output once the implementation lands.

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
`apm audit --ci` but is not yet wired through `apm install` / `apm update`.

### 3. Inheritance and composition

Policy resolves through the same three-level chain: enterprise hub -> org ->
repo override. The merge is **tighten-only** (see "Inheritance rules" above).
Install-time enforcement uses the same resolved effective policy as
`apm audit --ci`.

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
| `apm update` | NEW — same gate as `apm install` |
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

### 7. How install-time enforcement prevents disallowed sources

```
TODO: snippet from W4 live matrix — L2 (deny-list block).
TODO: snippet from W4 live matrix — L4 (required missing, block).
TODO: snippet from W4 live matrix — L13 (transitive dep blocked).
```

### 8. Escape hatches

**Non-bypass contract:** every hatch below is single-invocation, is not
persisted, and does **NOT** change CI behaviour. `apm audit --ci` will still
fail the PR for the same policy violation.

| Hatch | Scope |
|-------|-------|
| `--no-policy` | On `apm install`, `apm install <pkg>`, `apm install --mcp`, `apm update`. Skips discovery + enforcement; loud warning. |
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
  invocation; **do NOT block the install**. Fail-open default, ratified to
  keep developers unblocked when GitHub is unreachable.
- **Garbage response** (HTTP 200 with non-YAML body, e.g. captive portal):
  treated as fetch failure — warn loudly, cache fallback if present, otherwise
  proceed without enforcement.

Orgs needing fail-closed semantics: track the planned
`policy.fetch_failure: warn|block` schema knob (follow-up issue link TBD).

### 10. Troubleshooting

```
TODO (W3-docs-final): every install-time policy error + actionable next step.
Cover at minimum:
  - auth failure       -> check `gh auth status` and GITHUB_APM_PAT
  - unreachable        -> retry, check VPN/firewall, or use --no-policy
  - malformed policy   -> contact org admin to fix <source>
  - blocked dependency -> remove <dep>, contact admin, or --no-policy
  - missing required   -> add <dep> to apm.yml or contact admin
  - target mismatch    -> adjust compilation.target or --target flag
```

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
