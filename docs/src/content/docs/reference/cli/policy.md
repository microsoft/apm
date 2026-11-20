---
title: apm policy
description: Inspect and diagnose the resolved APM organization policy
sidebar:
  order: 19
---

Diagnostic surface for the policy enforcement layer. Lets admins and CI
pipelines verify what `apm-policy.yml` was discovered, how fresh the
cache is, the resolved `extends:` chain, and the count of effective
rules -- without running a full `apm install` or `apm audit`.

## Synopsis

```bash
apm policy status [--policy-source SOURCE] [--no-cache]
                  [-o table|json] [--json] [--check] [-g]
apm policy explain [-g] PACKAGE
```

## Description

`apm policy` groups diagnostic subcommands for the organization-level
policy APM resolves at install / audit time. The group exposes two
subcommands: `status` (the policy-chain view) and `explain` (the
per-package executable-trust decision).

The command is **always exit 0 by default**. Discovery failures are
reported in the output (table or JSON), never via process exit code, so
it stays safe for human inspection and SIEM ingestion. Pass `--check`
to opt into a CI-friendly contract that exits `1` when no usable policy
is resolved.

For the `apm-policy.yml` schema and enforcement model, see
[Policy reference](../../policy-schema/).

## Subcommands

### `apm policy status`

Render a diagnostic snapshot of the active APM policy: discovery
outcome, source, enforcement level, cache age, resolved `extends:`
chain, and effective rule counts.

```bash
apm policy status [OPTIONS]
```

| Flag | Description |
|---|---|
| `--policy-source SOURCE` | Override discovery. Accepts `org` (auto-discover from the project's git remote), `owner/repo` (defaults to github.com), an `https://` URL, or a local file path. |
| `--no-cache` | Force a fresh fetch; skip the policy cache. |
| `-o`, `--output {table,json}` | Output format. Default: `table`. |
| `--json` | Emit JSON. Alias of `-o json`. |
| `--check` | Exit `1` when no usable policy is resolved (any `outcome` other than `found`). Default exit is always `0`. |
| `-g`, `--global` | Show the user-scope (`apm install -g`) posture. User scope has no git remote, so no org policy is discovered: `outcome` is `no_git_remote` and `source` is `none`. |

Direct policy URLs should not embed credentials. Cache metadata and command
output omit URL userinfo, query strings, and fragments.

#### Output fields

The table and JSON renderers expose the same fields:

| Field | Meaning |
|---|---|
| `scope` | `project`, or `user` with `-g`. |
| `outcome` | Discovery result, one of: `found`, `absent`, `empty`, `disabled`, `no_git_remote`, `cached_stale`, `cache_miss_fetch_fail`, `garbage_response`, `malformed`, `hash_mismatch`, `incomplete_chain`, `ambiguous_git_remote` (several git remotes and no `origin`; always blocks). Every command that reads policy handles an outcome the same way. |
| `source` | Resolved source label (e.g. `org:owner/repo`, `url:https://...`, `file:./path`). |
| `enforcement` | Effective enforcement mode: `block`, `warn`, or `off`. |
| `cache_age_human` | Age of the cached policy entry, with stale / refresh-failure context. |
| `cache_stale`, `cached`, `cache_age_seconds` | Raw cache state (JSON only). |
| `extends_chain` | Resolved `extends:` ancestors of the leaf policy. |
| `rule_summary` | Human one-liners for non-empty rule axes. |
| `rule_counts` | Per-axis integer counts. `-1` means "no opinion" (allow-list omitted); `0` means "explicitly empty". JSON only. |
| `fetch_error`, `error` | Populated when discovery or refresh failed. |
| `warnings` | Non-fatal warnings raised while loading the policy. |

#### Exit codes

| Mode | `outcome=found` | Anything else |
|---|---|---|
| default | `0` | `0` |
| `--check` | `0` | `1` |

To gate on rule violations rather than resolvability, use
[`apm audit --ci`](../audit/) instead.

### `apm policy explain`

Explain the effective executable-trust decision for a single installed
package. For each executable type the package declares, it prints whether
that primitive is allowed, the deciding precedence layer (a compound
label such as `org-deny`, `project-allow`, or `default-deny`), and any
lower-authority layers that decision shadowed. This is
the per-package companion to `apm policy status` (the policy-chain view) and
the fleet-level executable-trust drift check in `apm doctor`.

```bash
apm policy explain [-g] PACKAGE
```

| Argument or flag | Description |
|---|---|
| `PACKAGE` | Installed package, by identity (`owner/repo`), install spec (`plugin@marketplace`) or local path. A reference that matches no installed package exits 1. |
| `-g`, `--global` | Explain a package `apm install -g` installed, from any directory. |

For a package blocked by an `apm.yml` or `--user` deny, the output names that
deny entry and how to lift it, rather than suggesting an approval.

The effective decision follows the deny-wins precedence: an organization
`executables.deny` / `deny_all` is the ceiling no project or user grant can
widen. See [Executable approval](../approve/) for the trust model and
[apm-policy.yml schema](../../policy-schema/#executables) for the
`executables` ceiling.

## Examples

```bash
# Show resolved policy state for the current project
apm policy status

# Force a fresh fetch (bypass cache)
apm policy status --no-cache

# Machine-readable JSON for SIEM or CI inspection
apm policy status --json

# Inspect a draft policy without committing it
apm policy status --policy-source ./draft-policy.yml

# Inspect an explicit org policy by repo
apm policy status --policy-source acme-corp/apm-policies

# CI pre-check: fail the job when no usable policy is resolved
apm policy status --check

# Explain the effective executable-trust decision for an installed package
apm policy explain owner/repo
```

Sample table output:

```
APM Policy Status
-----------------
  Outcome          found
  Source           org:acme-corp/apm-policies
  Enforcement      block
  Cache age        4m ago
  Extends chain    acme-corp/apm-baseline
  Effective rules  3 dependency denies; 2 mcp denies; 1 required manifest fields
  Warnings         none
```

## Related

- [`apm install`](../install/) -- enforces policy during dependency
  resolution; honors `--no-policy` to bypass.
- [`apm audit`](../audit/) -- gate on rule violations with `--ci`;
  complements `apm policy status --check`.
- [Policy reference](../../policy-schema/) -- canonical
  `apm-policy.yml` schema and enforcement semantics.
- [Governance deep-dive](../../../enterprise/security/) --
  how policy fits the broader enterprise governance model.
- [APM policy: getting started](../../../enterprise/apm-policy/)
  -- author and publish your first `apm-policy.yml`.
- [Enforce in CI](../../../enterprise/enforce-in-ci/) -- wire `audit`
  and `policy status` into pipelines.
