---
title: "apm-policy.yml"
description: "One org-wide policy file with tighten-only inheritance for AI agent dependencies, MCP servers, and compilation targets."
sidebar:
  order: 3
---

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
  trust_transitive: false  # do not auto-install MCPs from transitive deps
```

Twelve lines, three rules: only contoso and microsoft packages are allowed, untrusted-org is blocked outright, MCP transports are restricted, and MCPs from transitive packages require explicit opt-in.

---

## How enforcement happens

Policy is evaluated at two points. Both use the same policy file and the same merge semantics.

### Install time (preflight gate)

`apm install` reads the discovered policy before resolving dependencies. Violations halt the install with a non-zero exit code; nothing is written to disk. This protects developers who run `apm install` locally — they cannot accidentally deploy a denied package even without CI.

### CI time (audit gate)

`apm audit --ci --policy org` runs the same checks (plus 6 baseline lockfile checks) and is intended as a required status check on pull requests. It produces SARIF output that GitHub Code Scanning renders inline on the PR diff.

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

`apm install` halts before any file is written:

```
[x] Policy violation: dependency 'untrusted-org/random-skills' is denied by org policy
    Policy: contoso/.github/apm-policy.yml
    Rule:   dependencies.deny matches 'untrusted-org/**'
    Action: install aborted, no files deployed

Run `apm audit --ci --policy org` for full report. Override with `--no-policy` (not recommended).
```

In CI, `apm audit --ci --policy org` produces the same finding as a SARIF result. GitHub Code Scanning renders it inline on the PR diff with the offending line annotated. The PR cannot be merged until the violation is resolved or the policy is amended through the org's own change-management process.

---

## Forensics

When an incident review asks "what was running last Tuesday?", the answer is in the lockfile, not in the policy:

```bash
git show <commit>:apm.lock.yaml | grep resolved_commit
git log --oneline apm.lock.yaml
```

The policy says what is allowed; the lockfile records what was actually deployed. Both are git-tracked, both are reviewable, both reconstruct any historical state with one git command. See [Lock file as audit trail](../governance/#lock-file-as-audit-trail) in the Governance guide.

---

## Next steps

- **Schema and every field** — [Policy Reference](../policy-reference/)
- **Wire it into CI with SARIF** — [CI Policy Enforcement](../../guides/ci-policy-setup/)
- **Broader governance model** (lock files, audit trails, compliance scenarios) — [Governance & Compliance](../governance/)
