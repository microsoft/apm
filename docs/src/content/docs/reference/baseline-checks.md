---
title: Baseline CI checks
description: Reference list of every check that apm audit --ci runs against a project, with failure conditions, exit-code impact, and remediation.
sidebar:
  order: 7
---

`apm audit --ci` runs a fixed set of baseline checks against the project on
disk. Baseline checks are always on -- they need no `apm-policy.yml`. With
policy discovery active, the org policy contributes additional checks on
top (see [`apm policy`](../cli/policy/) and the
[policy schema](../policy-schema/)).

This page documents the baseline set defined in
`src/apm_cli/policy/ci_checks.py`. For the command surface, see
[`apm audit`](../cli/audit/). For the CI wiring, see
[Enforce in CI](../../enterprise/enforce-in-ci/).

## Exit-code contract

| Outcome | Exit code |
|---|---|
| All baseline checks pass (and any policy + drift checks pass) | `0` |
| Any baseline, policy, or drift check fails | `1` |

`--no-fail-fast` runs every check before exiting; the default stops at the
first failure to skip expensive I/O.

## At a glance

| ID | Severity | Source | Always on? |
|---|---|---|---|
| `manifest-parse` | block | `ci_checks.py` | only when `apm.yml` cannot be parsed |
| `lockfile-exists` | block | `ci_checks.py` | yes |
| `ref-consistency` | block | `ci_checks.py` | yes |
| `deployed-files-present` | block | `ci_checks.py` | yes |
| `no-orphaned-packages` | block | `ci_checks.py` | yes |
| `skill-subset-consistency` | block | `ci_checks.py` | yes |
| `config-consistency` | block | `ci_checks.py` | yes |
| `content-integrity` | block | `ci_checks.py` | yes |
| `includes-consent` | advisory | `ci_checks.py` | yes (advisory; promotable by policy) |
| `drift` | block | `_check_drift` in `ci_checks.py`, invoked from `commands/audit.py` | yes, unless `--no-drift` |

Policy checks (`dependency-allowlist`, `dependency-denylist`,
`required-packages`, `required-packages-deployed`,
`required-package-version`, `transitive-depth`, and others in
`policy/policy_checks.py`) only run when an `apm-policy.yml` is resolved
through discovery or `--policy`. They are out of scope for this page; see
the [policy schema](../policy-schema/).

## Baseline checks

### `manifest-parse`

- **What it verifies.** That `apm.yml`, if present, is valid YAML and a valid APM manifest.
- **Fails when.** The file exists but cannot be parsed (`yaml.YAMLError`, `ValueError`, or `OSError`).
- **Effect.** Skips every other check and exits `1`. This is the only check that runs before the lockfile gate.
- **Remediation.** Fix the YAML syntax error reported in the message and re-run.

### `lockfile-exists`

- **What it verifies.** That `apm.lock.yaml` is present whenever the project has APM or MCP dependencies, or whenever an existing lockfile records local content under the synthesized self-entry.
- **Fails when.** `apm.yml` declares dependencies but no lockfile is on disk.
- **Effect.** Subsequent checks are skipped (the lockfile is required input).
- **Remediation.** Run `apm install` to generate `apm.lock.yaml` and commit it.

### `ref-consistency`

- **What it verifies.** That every dependency's `reference` in `apm.yml` matches the `resolved_ref` recorded in the lockfile.
- **Fails when.** A manifest ref differs from the lockfile entry, or the manifest declares a dependency that is missing from the lockfile.
- **Remediation.** Run `apm install` so the lockfile re-resolves to the manifest, then commit `apm.lock.yaml`.

### `deployed-files-present`

- **What it verifies.** That every path in each lockfile entry's `deployed_files` exists on disk under the project root.
- **Fails when.** One or more deployed files are missing (e.g. a developer ran `apm install` then deleted integrated files, or skipped install entirely).
- **Remediation.** Run `apm install` to restore the integrated files.

### `no-orphaned-packages`

- **What it verifies.** That every dependency in the lockfile is still declared in `apm.yml`. The synthesized self-entry is excluded.
- **Fails when.** The lockfile holds a package that the manifest no longer lists.
- **Remediation.** Run `apm install` to prune the orphan, then commit `apm.lock.yaml`.

### `skill-subset-consistency`

- **What it verifies.** That the `skills:` selection in `apm.yml` for each `skill_bundle` dependency matches the `skill_subset` recorded in the lockfile.
- **Fails when.** The sorted manifest skill list differs from the sorted lockfile `skill_subset` for any skill bundle.
- **Remediation.** Run `apm install` to regenerate the lockfile against the current selection.

### `config-consistency`

- **What it verifies.** That MCP server configs derived from `apm.yml` match the `mcp_configs` baseline stored in the lockfile.
- **Fails when.** A server's resolved config differs from the lockfile, a server is in the lockfile but not the manifest, or a server is in the manifest but not the lockfile.
- **Remediation.** Run `apm install` to reconcile the MCP configuration.

### `content-integrity`

- **What it verifies.** Two signals across every deployed file (including local `.apm/` content via the synthesized self-entry):
  1. Critical hidden Unicode (tag characters, bidi overrides, variation selectors 17-256, and similar steganographic markers).
  2. SHA-256 drift between the on-disk content and the hash recorded in `deployed_file_hashes` at install time.
- **Fails when.** Any deployed file contains a critical Unicode finding or its hash no longer matches the lockfile entry. Missing files are intentionally not reported here -- `deployed-files-present` owns that signal. Symlinks and entries without a recorded hash are skipped.
- **Remediation.** Run `apm audit --strip` to clean Unicode findings, and `apm install` to restore hash-drifted files. Both may be needed.

### `includes-consent`

- **What it verifies.** That projects deploying local content (the lockfile's `local_deployed_files` is non-empty) declare an `includes:` field in `apm.yml` for explicit governance.
- **Fails when.** Never -- this check is advisory and always passes. The message advises adding `includes: auto` (or an explicit list) when local content is deployed without a declaration.
- **Promote to a block.** Set `manifest.require_explicit_includes` in `apm-policy.yml` to make missing `includes:` fail the policy layer.
- **Remediation.** Add `includes: auto` to `apm.yml`, or list the paths explicitly.

### `drift`

- **What it verifies.** That the working tree matches what an install from the current lockfile would produce. The check replays the install pipeline into a scratch tree and diffs the result against the project.
- **Fails when.** Any deployed file differs from the replay output (hand-edits, missing integrations, orphaned files), or the cache is cold and `apm install` has not populated `apm_modules` (the replay is cache-only by design so audit stays deterministic).
- **Skip with.** `apm audit --ci --no-drift` (reduces coverage; reserve for performance-constrained CI loops).
- **Remediation.** Run `apm install` to restore the deployed state, or revert the hand-edit. For a cache-miss failure, the same `apm install` warms the cache.

## Run order and fail-fast

The aggregate runner in `run_baseline_checks` evaluates checks in this order: `manifest-parse` (only when `apm.yml` is unparseable), `lockfile-exists`, `ref-consistency`, `deployed-files-present`, `no-orphaned-packages`, `skill-subset-consistency`, `config-consistency`, `content-integrity`, `includes-consent`. Drift is invoked separately by the audit command after the baseline batch.

With fail-fast on (the default), the runner stops at the first failing check. `apm audit --ci --no-fail-fast` evaluates every check so the report lists every problem at once.

## Related

- [`apm audit`](../cli/audit/) -- the command surface and modes.
- [`apm policy`](../cli/policy/) -- inspect, validate, and resolve org policy.
- [Policy schema](../policy-schema/) -- the policy-gated checks layered on top of this baseline.
- [Enforce in CI](../../enterprise/enforce-in-ci/) -- wiring the gate into branch protection.
