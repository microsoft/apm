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

This page is the mental model. For the full schema, see the [Policy Reference](../policy-reference/). For wiring it into CI, see the [CI Policy Enforcement guide](../../guides/ci-policy-setup/).

---

## What it is

One YAML file. Lives at `<org>/.github/apm-policy.yml`. Auto-discovered by `apm install` and `apm audit --ci --policy org` from your project's git remote.

It declares:

- Allow / deny lists for **dependency sources** (org globs, package patterns).
- Allow / deny lists for **MCP servers** and their transports.
- Required packages (e.g. an org-wide standards package every repo must consume).
- Compilation target rules (which agent runtimes are permitted).
- Manifest rules (required `apm.yml` fields, allowed content types).
- Behaviour for unmanaged files in governed directories.

It does **not** scan code semantics or behave like an antivirus. It enforces declarations against an allow/deny list before APM writes any file.

---

## Where it lives

The canonical location is the `.github` repository under your org:

```
<org>/
  .github/
    apm-policy.yml         # auto-discovered by every repo in <org>
```

When `apm install` or `apm audit --ci --policy org` runs in a project, APM resolves the org from the project's git remote and fetches `<org>/.github/apm-policy.yml` (cached locally, default 1 hour TTL).

Alternative sources, useful for testing or non-GitHub setups:

- **Local file** — `apm audit --ci --policy ./apm-policy.yml`
- **HTTPS URL** — `apm audit --ci --policy https://example.com/apm-policy.yml`

See [Alternative policy sources](../../guides/ci-policy-setup/#alternative-policy-sources) for details.

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
    allow: [http, stdio]   # block sse and streamable-http
```

Three rules: only contoso and microsoft packages are allowed, untrusted-org is blocked outright, and MCP transports are restricted to `http` and `stdio`.

> **Note on transitive MCPs:** the `mcp.trust_transitive` policy field is currently parsed but not enforced — the actual gate is the `--trust-transitive-mcp` CLI flag (defaults to deny). See [Governance Guide §5a](../governance-guide/#5a-what-does-not-enforce-policy) for the full list of parsed-but-not-enforced fields.

---

## How enforcement happens

Policy is evaluated at two points. Both use the same policy file and the same merge semantics.

### Install time (preflight gate)

`apm install` resolves the dependency tree, then runs the policy gate against the resolved set, then writes any files. A blocking violation halts the install with a non-zero exit code; nothing is written to disk. This protects developers who run `apm install` locally — they cannot accidentally deploy a denied package even without CI.

> **Bypass note:** `apm install --no-policy` and the `APM_POLICY_DISABLE=1` environment variable skip this gate locally. They also skip 16 of the 22 checks when `apm audit --ci` runs in the same shell. See the [Governance Guide bypass contract](../governance-guide/#7-the-bypass--non-bypass-contract) for the full surface.

### CI time (audit gate)

`apm audit --ci --policy org` runs the same checks (plus 7 baseline lockfile checks) and is intended as a required status check on pull requests. It produces SARIF output that GitHub Code Scanning renders inline on the PR diff.

For setup, see [CI Policy Enforcement](../../guides/ci-policy-setup/).

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

Inheritance chains up to **5 levels** are supported, so an enterprise hub policy can flow into an org policy, which flows into a team policy, which flows into a repo override:

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

For lockfile-based forensic recipes, see [Lock file as audit trail](../governance/#lock-file-as-audit-trail) and the [Governance Guide §13: enforcement audit log](../governance-guide/#13-the-enforcement-audit-log).

---

## Next steps

- **Schema and every field** — [Policy Reference](../policy-reference/)
- **Wire it into CI with SARIF** — [CI Policy Enforcement](../../guides/ci-policy-setup/)
- **Broader governance model** (lock files, audit trails, compliance scenarios) — [Governance & Compliance](../governance/)
