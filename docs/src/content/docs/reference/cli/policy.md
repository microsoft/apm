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
                  [-o table|json] [--json] [--check]
```

## Description

`apm policy` groups diagnostic subcommands for the organization-level
policy APM resolves at install / audit time. Today the group exposes a
single subcommand, `status`.

The command is **always exit 0 by default**. Discovery failures are
reported in the output (table or JSON), never via process exit code, so
it stays safe for human inspection and SIEM ingestion. Pass `--check`
to opt into a CI-friendly contract that exits `1` when no usable policy
is resolved.

For the `apm-policy.yml` schema and enforcement model, see
[Policy reference](../../policy-reference/).

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

#### Output fields

The table and JSON renderers expose the same fields:

| Field | Meaning |
|---|---|
| `outcome` | Discovery result: `found`, `absent`, `disabled`, `no_git_remote`, `cache_miss_fetch_fail`, ... |
| `source` | Resolved source label (e.g. `org:owner/repo`, `url:https://...`, `file:./path`). |
| `enforcement` | Effective enforcement mode: `block`, `warn`, or `off`. |
| `cache_age_human` | Age of the cached policy entry, with stale / refresh-failure context. |
| `cache_stale`, `cached`, `cache_age_seconds` | Raw cache state (JSON only). |
| `extends_chain` | Resolved `extends:` ancestors of the leaf policy. |
| `rule_summary` | Human one-liners for non-empty rule axes. |
| `rule_counts` | Per-axis integer counts. `-1` means "no opinion" (allow-list omitted); `0` means "explicitly empty". JSON only. |
| `fetch_error`, `error` | Populated when discovery or refresh failed. |

#### Exit codes

| Mode | `outcome=found` | Anything else |
|---|---|---|
| default | `0` | `0` |
| `--check` | `0` | `1` |

To gate on rule violations rather than resolvability, use
[`apm audit --ci`](../audit/) instead.

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
```

## Related

- [`apm install`](../install/) -- enforces policy during dependency
  resolution; honors `--no-policy` to bypass.
- [`apm audit`](../audit/) -- gate on rule violations with `--ci`;
  complements `apm policy status --check`.
- [Policy reference](../../policy-reference/) -- canonical
  `apm-policy.yml` schema and enforcement semantics.
- [Governance overview](../../../enterprise/governance-overview/) --
  how policy fits the broader enterprise governance model.
- [APM policy: getting started](../../../enterprise/apm-policy-getting-started/)
  -- author and publish your first `apm-policy.yml`.
- [Enforce in CI](../../../enterprise/enforce-in-ci/) -- wire `audit`
  and `policy status` into pipelines.
