---
title: Pilot a New Policy
description: Roll out a new apm-policy.yml rule without breaking every repo on day one. The warn-then-block playbook for platform teams.
sidebar:
  order: 3
---

You wrote a new rule in `<org>/.github/apm-policy.yml`. Do not flip it to `block` yet. Half your repos already violate it -- you don't know which ones.

This page is the rollout sequence. Author in `warn`, measure, remediate, then escalate to `block`. Per-rule mode flips do not exist; the dial is the top-level `enforcement` field.

For the schema, see [Policy Reference](../policy-reference/). For the trust contract and bypass surfaces, see [Governance overview](../governance-overview/). For wiring the gate into pull requests, see [Enforce in CI](../enforce-in-ci/).

---

## 1. Why pilot first

A new `dependencies.deny` entry, a tighter `mcp.allow` glob, or a stricter `dependencies.max_depth` is retroactive. The next `apm install` in every consuming repo runs the new rule against dependencies that were legal yesterday.

If you flip straight to `enforcement: block`, every CI job on every repo that already has a violation fails on its next run. You will spend the day rolling back instead of rolling out.

Pilot first. Collect the violation list. Fix or grant exceptions. Then escalate.

---

## 2. The three modes

The top-level knob in `apm-policy.yml`:

```yaml
enforcement: warn   # warn | block | off
```

Verified in `src/apm_cli/policy/schema.py` and `policy/parser.py`:

| Mode | What `apm install` does | What `apm audit --ci` does |
|---|---|---|
| `off` | Skip policy enforcement entirely. | Skip policy checks (baseline lockfile checks still run). |
| `warn` | Run every check; emit `Policy warning: <dep> -- <reason>`; install proceeds. | Report violations; exit 0. |
| `block` | Run every check; emit `Policy violation: <dep> -- <reason>`; abort with `PolicyViolationError`. | Report violations; exit non-zero. |

There is no `dry-run` enforcement mode. `--dry-run` is a CLI flag on `apm install` that previews `Would be blocked by policy: <dep> -- <reason>` lines without writing to disk -- usable against any policy mode.

Per-rule modes exist only for two sub-fields: `mcp.self_defined` (`deny | warn | allow`) and `unmanaged_files.action` (`ignore | warn | deny`). Every other rule inherits the top-level `enforcement`.

---

## 3. The rollout sequence

### Step 1 -- Author in warn

Land the new rule with `enforcement: warn` in `<org>/.github/apm-policy.yml`:

```yaml
name: acme-org-policy
version: 1
enforcement: warn
dependencies:
  deny:
    - "legacy-org/*"        # the new rule under pilot
```

Merge through your `.github` repo's branch protection. Cache TTL defaults to 1 hour; consuming repos pick it up on their next `apm install`.

### Step 2 -- Read the warning telemetry

Three places surface violations:

1. **`apm install` output.** Each violation is written to `DiagnosticCollector` under `CATEGORY_POLICY` and printed in the end-of-install summary (`policy_violation` in `core/command_logger.py`).
2. **`apm audit --ci --policy org`** in CI. Same checks, structured exit code, and SARIF / JSON output for GitHub Code Scanning (`apm audit --ci --format sarif`). This is the canonical way to collect a fleet-wide violation list -- see [Enforce in CI](../enforce-in-ci/).
3. **`apm install --dry-run`.** Local preview without writing to disk. Useful for a developer reproducing a CI warning.

Run `apm audit --ci --policy org --format sarif` in every repo (a scheduled workflow is enough) and aggregate. Code Scanning will dedupe by rule and dependency.

### Step 3 -- Remediate

For each violating repo, pick one:

- **Upgrade or replace the dependency.** The default fix.
- **Grant an exception by relaxing the rule.** APM has no first-class waiver field. Inheritance is tighten-only (`policy/inheritance.py`), so a child repo cannot loosen a parent. The only way to exempt a repo is to relax the rule at the parent level -- typically by narrowing the `deny` glob or adding the specific package to `dependencies.allow`. Document the exception in the policy file itself; that file is your audit log.
- **Disable for the repo (escape hatch).** A consuming repo can pass `--no-policy` or set `APM_POLICY_DISABLE=1`. This is loud (`policy_disabled` always prints) and covers the whole policy, not one rule. Treat it as break-glass, not as a waiver mechanism.

### Step 4 -- Flip to block

Once the violation count is zero (or the surviving violations are documented exceptions), change one line:

```yaml
enforcement: block
```

The next `apm install` in any repo with an undetected violation will now fail closed. CI jobs that already passed under `warn` will continue to pass.

---

## 4. Per-rule vs whole-policy flips

There is no `dependencies.deny.enforcement: block` knob. The top-level `enforcement` applies uniformly to every check.

If you need a single rule to block while everything else only warns, you have two options:

- **Stage rules.** Land rule A in `warn`. When clean, merge rule B in `warn`. When both are clean, flip `enforcement: block`. Slower, but the policy file stays simple.
- **Split policies via `extends:`.** Author a strict child policy that `extends:` your org policy and lives in a specific scope (e.g., a sub-org's `.github/apm-policy.yml`). Inheritance is tighten-only, so the child can escalate `warn` to `block` for its repos only. See `policy/inheritance.py` and the [Governance overview](../governance-overview/) section on composition.

---

## 5. When to roll back

Flip back to `enforcement: warn` (or `off`) and merge through `.github` branch protection. The next `apm install` in every repo picks up the relaxed policy within the cache TTL (default 1 hour).

Roll back when:

- A violation surge reveals a category of legitimate use you missed.
- A transitive MCP dependency that you cannot patch on your timeline starts blocking installs.
- The policy file itself fails to fetch in a way that interacts badly with `policy.fetch_failure: block` -- see [Enforce in CI](../enforce-in-ci/) for the failure-mode matrix.

A rollback is cheap. A blocked org is not. If you are debating, roll back, fix forward, escalate again.
