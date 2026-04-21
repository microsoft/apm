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
