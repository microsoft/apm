---
applyTo: "src/apm_cli/**"
description: "Single canonical owner discipline: one authority per durable decision, guarded by a regression test + a static boundary check"
---

# Architecture discipline: one canonical owner per decision

APM is a pipeline of durable facts (targets, lock state, install
outcomes, compiled output, hook shapes, credentials, deployment
provenance). Most reliability bugs in this codebase have one shape:
the SAME decision was computed or enforced in more than one place, so
a fix on one path silently missed a sibling path. The cure is
structural, not case-by-case.

## The rule

Every durable decision, vocabulary, outcome, write, or contract has
exactly ONE canonical owner. Every call site routes THROUGH that owner
instead of re-deriving the answer locally.

- A "decision" is anything a reader must be able to trust is computed
  identically everywhere: the accepted target set, whether an install
  succeeded, the on-disk shape of a hook, the integrity hash of a
  deployed file, the resolved credential for a host.
- Adding a second place that computes or enforces the same decision is
  a "split authority" and is a defect even if it currently agrees --
  it WILL drift the next time one side is patched.

## Existing canonical owners -- route through these, do not re-derive

The `Owner path selectors` column is executable contract data. The
shepherd-driver owner-touch gate parses it directly; do not copy these
selectors into another table or script. Keep selectors repository-relative,
semicolon-delimited, and specific to the file(s) that own the fact.

<!-- canonical-owner-table:v1 -->
| Decision / fact | Canonical owner | Owner path selectors |
|---|---|---|
| Accepted target vocabulary | core/target_catalog.py | `src/apm_cli/core/target_catalog.py` |
| Host + credential resolution | core/auth.py (AuthResolver), core/host_providers.py | `src/apm_cli/core/auth.py`; `src/apm_cli/core/host_providers.py` |
| Runtime descriptors | runtime/registry.py | `src/apm_cli/runtime/registry.py` |
| User-facing output / diagnostics | CommandLogger / console owner | `src/apm_cli/core/command_logger.py`; `src/apm_cli/utils/console.py` |
| Compiled-output writes (atomic) | CompiledOutputWriter | `src/apm_cli/compilation/output_writer.py` |
| Deployment provenance / state | deployment_ledger.py | `src/apm_cli/core/deployment_ledger.py` |
| Target-scoped deployed-file contraction | install/manifest_reconcile.py (reconcile_target_deployed_files) | `src/apm_cli/install/manifest_reconcile.py` |
| Install success / failure outcome | the canonical install-outcome path | `src/apm_cli/install/outcome.py` |
| Neutral hook shape -> per-target native | the neutral hook IR + per-target integrators | `src/apm_cli/integration/hook_ir.py`; `src/apm_cli/integration/hook_native_formats.py`; `src/apm_cli/integration/hook_integrator.py` |
| File-level deploy / sync / cleanup | BaseIntegrator (see integrators.instructions.md) | `src/apm_cli/integration/base_integrator.py` |
| Windows stable executable path | install.ps1 ($currentDir / $currentExe) | `install.ps1` |
| Git repository cache-key normalization | cache/url_normalize.py (normalize_repo_url / cache_shard_key) | `src/apm_cli/cache/url_normalize.py` |
| Cached policy shape | policy/discovery.py (_policy_to_dict via _serialize_policy) | `src/apm_cli/policy/discovery.py` |
| Post-uninstall dependency reachability | deps/reachability.py (compute_forward_reachable_keys) | `src/apm_cli/deps/reachability.py` |
| GitHub API throttle classification | deps/github_rate_limit.py | `src/apm_cli/deps/github_rate_limit.py` |
| Bootstrap project-name validation and fallback | core/project_name.py (resolve_bootstrap_project_name) | `src/apm_cli/core/project_name.py` |
<!-- /canonical-owner-table -->

If you are about to compute one of these locally, stop and call the
owner. If the owner is missing a case you need, EXTEND the owner --
never fork it.

## When you centralize or fix a split-authority bug: dual guardrail

A fix is not done until the split cannot silently return. Add BOTH:

1. A behavioral **regression test** (hermetic, under tests/) that
   encodes the exact symptom and fails before / passes after.
2. A **static boundary guard** so a future contributor cannot re-add a
   second owner: extend scripts/lint-architecture-boundaries.sh and the
   matching tests/integration/test_architecture_*.py suite.

The scripts/lint-architecture-boundaries.sh check is wired into CI (the
Lint job) alongside the auth-signal guard. Treat a new authority the
same way: give it a guard line.

## Review lens

When reviewing or authoring a change, ask: "Does this compute or
enforce a decision the codebase already owns elsewhere?" If yes, the
change must route through the owner, and a new parallel path is a
blocking finding, not a nit.
