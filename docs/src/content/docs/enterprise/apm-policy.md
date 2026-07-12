---
title: "Policy Files"
description: "One org-wide policy file with tighten-only inheritance for AI agent dependencies, MCP servers, and compilation targets."
sidebar:
  order: 7
---

For the full enterprise rollout playbook and bypass contract, see the [Governance Guide](../governance-guide/).

:::caution[Experimental Feature]
The `apm-policy.yml` schema, inheritance, and discovery ship today and are usable for testing and feedback. Policy enforcement at install time and via `apm audit --ci --policy` is an early preview. Fields, defaults, and check behaviour may change based on community input. Pin your policy to a specific APM version and watch the [CHANGELOG](https://github.com/microsoft/apm/blob/main/CHANGELOG.md) for breaking changes.
:::

`apm-policy.yml` is a single YAML file that defines what AI agent dependencies, MCP servers, and compilation targets are allowed across an organization. It is the governance pillar of APM — the file your security team owns and your repos inherit.

This page is the mental model. For the full schema, see the [Policy Reference](../policy-reference/). For wiring it into CI, see the [Enforce in CI guide](../enforce-in-ci/).

---

## What it is

One YAML file. Lives in your org's policy repo (`apm-policy.yml`). Auto-discovered by `apm install` and `apm audit --ci --policy org` from your project's git remote.

It declares:

- Allow / deny lists for **dependency sources** (org globs, package patterns).
- Allow / deny lists for **MCP servers** and their transports.
- Required packages (e.g. an org-wide standards package every repo must consume).
- Compilation target rules (which agent runtimes are permitted).
- Manifest rules (required `apm.yml` fields and explicit `includes:` requirements).
- Behaviour for unmanaged files in governed directories.
- Registry-source rules for requiring named registries and blocking non-registry sources.
- Install-time audit/integrity rules under `security.*`, plus executable-trust controls under `executables.*`.

It does **not** scan code semantics or behave like an antivirus. It enforces declarations against an allow/deny list before APM writes any file.

The policy schema addresses install-time gates exclusively. It has no fields for runtime permissions or agent sandboxing. See [Governance deep-dive](/apm/enterprise/governance-guide/) for how APM policy coexists with harness configuration.

---

## Where it lives

APM discovers your org-level policy by checking candidate repos in this order -- the first one found wins:

| Priority | Repo name | Valid on |
|----------|-----------|---------|
| 1 | `.github-private` | GitHub and GitHub API-compatible hosts |
| 2 | `.github` | GitHub and GitHub API-compatible hosts |
| 3 | `.apm` | GitHub and GitHub API-compatible hosts |
| 4 | `_apm` | GitHub API-compatible hosts and Azure DevOps |

Azure DevOps does not allow repository names starting or ending with a period, so only `_apm` is tried on ADO hosts. ADO requires repositories to live inside projects; the convention uses `_apm` for both the project and repo:

```
<org>/
  _apm/              # ADO project
    _apm/            # repo inside the project
      apm-policy.yml
```

On GitHub and GitHub API-compatible hosts, the `.github-private` repo is preferred because policy files typically contain internal governance rules that should not be public. If `.github-private` does not exist, APM falls back to `.github`:

```
<org>/
  .github-private/
    apm-policy.yml         # auto-discovered by every repo in <org>
```

When `apm install` or `apm audit --ci --policy org` runs in a project, APM resolves the org from the project's git remote and searches the candidate repos above (cached locally, default 1 hour TTL).

Alternative sources, useful for testing or non-GitHub setups:

- **Local file** — `apm audit --ci --policy ./apm-policy.yml`
- **HTTPS URL** — `apm audit --ci --policy https://example.com/apm-policy.yml`

See [Enforce in CI](../enforce-in-ci/) for alternative policy sources and CI wiring details.

---

## A minimal policy

```yaml
name: "Contoso Engineering Policy"
version: "1.0.0"
enforcement: block         # warn | block | off

dependencies:
  allow:
    - "contoso/**"
    - "microsoft/*"
  deny:
    - "untrusted-org/**"

mcp:
  transport:
    allow: [http, stdio]   # explicit allow list: blocks sse and streamable-http
```

Three rules: only contoso and microsoft packages are allowed, untrusted-org is blocked outright, and MCP transports are restricted to `http` and `stdio`. With no `mcp.transport.allow` set, all transports are permitted by default; the example above shows how to restrict.

> **Note on transitive MCPs:** the `mcp.trust_transitive` policy field is currently parsed but not enforced — the actual gate is the `--trust-transitive-mcp` CLI flag (defaults to deny). See [Governance Guide §5a](../governance-guide/#5a-what-does-not-enforce-policy) for the full list of parsed-but-not-enforced fields.

---

## How enforcement happens

Policy is evaluated at two points. Both use the same policy file and the same merge semantics.

### Install time (preflight gate)

`apm install` resolves the dependency tree, then runs the policy gate against the resolved set, then writes any files. A blocking violation halts the install with a non-zero exit code; nothing is written to disk. This protects developers who run `apm install` locally — they cannot accidentally deploy a denied package even without CI.

> **Bypass note:** `apm install --no-policy` and the `APM_POLICY_DISABLE=1` environment variable skip this gate locally. The env var also skips all 20 policy checks when `apm audit --ci` runs in the same shell; the 8 baseline lockfile checks still run. See the [Governance Guide bypass contract](../governance-guide/#7-the-bypass--non-bypass-contract) for the full surface.

### CI time (audit gate)

`apm audit --ci --policy org` runs the same checks (plus 8 baseline lockfile checks) and is intended as a required status check on pull requests. It produces SARIF output that GitHub Code Scanning renders inline on the PR diff.

For setup, see [Enforce in CI](../enforce-in-ci/).

---

## Tighten-only inheritance

A repo can have its own `apm-policy.yml` that **extends** the org policy. Children can only **tighten** rules, never relax them. This means a repo can be more restrictive than the org, but cannot widen what the org has allowed.

The merge rules in plain English:

| Field | Merge rule (parent + child) |
|-------|----------------------------|
| `allow` lists | **intersect** — the child sees only entries present in both |
| `deny` lists | **union** — the child adds to the parent's deny |
| `max_depth` | **min(parent, child)** — whichever is smaller wins |
| `trust_transitive` | **parent AND child** — both must allow it |

The `enforcement` field escalates: `off` < `warn` < `block`. A child can move enforcement from `warn` to `block`, never the reverse.

The canonical chain is three semantic levels (enterprise hub -> org policy -> repo override), with a maximum chain depth of **5** to allow intermediate `extends:` jumps. An example longer chain:

```
Enterprise hub  ->  Org policy  ->  Team policy  ->  Repo override
```

The full merge table for every field (including `require_resolution`, `mcp.self_defined`, `manifest.scripts`, and `unmanaged_files.action`) is in the [Policy Reference: Inheritance](../policy-reference/#inheritance) section.

---

## What a violation looks like

A developer adds a denied package to `apm.yml`:

```yaml
dependencies:
  apm:
    - untrusted-org/random-skills
```

`apm install` halts before any file is written. The CLI emits a single-line violation followed by a remediation hint:

```
[x] Policy violation: untrusted-org/random-skills -- denied by pattern: untrusted-org/**
    Run `apm audit --ci --policy org` for the full report.
```

Exit code is non-zero so CI fails. Run `apm audit --ci --policy org` (in CI or locally) for the full SARIF report including which policy file in the inheritance chain produced the rule.

In CI, `apm audit --ci --policy org` produces the same finding as a SARIF result. GitHub Code Scanning renders it inline on the PR diff with the offending line annotated. The PR cannot be merged until the violation is resolved or the policy is amended through the org's own change-management process.

---

## Forensics

For lockfile-based forensic recipes, see the [Governance Guide §13: enforcement audit log](../governance-guide/#13-the-enforcement-audit-log).

---

## Next steps

- **Schema and every field** — [Policy Reference](../policy-reference/)
- **Wire it into CI with SARIF** — [Enforce in CI](../enforce-in-ci/)
- **Broader governance model** (lock files, audit trails, compliance scenarios) -- [Governance Guide](../governance-guide/)

---

## Get started in 20 minutes

This is nothing-to-working-policy. By the end you have a file in your org's `.github-private` repo (or `.github` if you prefer public policy) that blocks one bad package across every repo.

### 1. Write the minimal policy

Copy this into `<your-org>/.github-private/apm-policy.yml` (or `<your-org>/.github/apm-policy.yml`). It blocks one package family and starts in `warn` so you do not break anyone on day one:

```yaml
name: "Acme baseline policy"
version: "1.0.0"
enforcement: warn        # warn | block | off

dependencies:
  deny:
    - "untrusted-org/**"
```

Commit and push. On the next `apm install` in any repo in the org, a user who depends on `untrusted-org/anything` sees a warning. When you are ready to enforce, flip `enforcement: block`.

### 2. Preview before you commit

Point at a policy explicitly to test it before it lands in your policy repo (`.github-private` or `.github`):

```bash
apm policy status --policy-source ./apm-policy.yml
apm policy status --policy-source contoso/governance     # any owner/repo
apm audit --ci --policy ./apm-policy.yml
```

`apm install` has no `--policy` flag — discovery runs automatically from your git remote. Use `--no-cache` to force a fresh fetch.

### 3. The schema in one screen

Every current non-deprecated top-level key. Field names come straight from the schema -- do not invent others.

```yaml
name: ""                  # display name
version: ""               # your policy version
extends: null             # "org" | "<owner>/<repo>" | "https://..."
enforcement: warn         # warn | block | off
fetch_failure: warn       # warn | block (when policy can't be fetched)
cache:
  ttl: 3600               # seconds

dependencies:
  allow: null             # null = no opinion; [] = nothing allowed
  deny: []
  require: []             # packages every repo must include
  require_resolution: project-wins   # project-wins | policy-wins | block
  max_depth: 50
  require_pinned_constraint: false   # true = ban unbounded version ranges

mcp:
  allow: null
  deny: []
  transport:
    allow: null           # stdio | sse | http | streamable-http
  self_defined: warn      # allow | warn | deny (inline MCPs in apm.yml)
  trust_transitive: false

compilation:
  target:
    allow: null           # claude | copilot | cursor | opencode | codex | gemini | windsurf | kiro | agent-skills
    enforce: null
  strategy:
    enforce: null         # distributed | single-file
  source_attribution: false

manifest:
  required_fields: []
  scripts: allow          # allow | deny
  content_types: null     # {allow: [...]}
  require_explicit_includes: false

unmanaged_files:
  action: ignore          # ignore | warn | deny
  directories: []
registry_source:
  require: []             # registry names every dep must use
  allow_non_registry: true
security:
  audit: {}               # on_install, fail_on_drift, external scanners
  integrity: {}           # require_hashes
executables: {}           # deny_all, deny, require, recommend, enforce (accepted; degrades to recommend)
```

Allow-list semantics: `null` means "no opinion", an empty list means "explicitly allow nothing", a populated list means "only these". Deny and require lists accumulate. Per-field detail is in the [Policy Reference](../policy-reference/).

### 4. Roll it out

You have a policy that warns on one bad case. Wire `apm audit --ci --policy org` into your pipeline so CI gates even when a developer passes `--no-policy` locally — see [Enforce in CI](../enforce-in-ci/). Pilot in `warn`, observe, then flip to `block`.
