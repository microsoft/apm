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

| Decision / fact | Canonical owner |
|---|---|
| Accepted target vocabulary | core/target_catalog.py |
| Host + credential resolution | core/auth.py (AuthResolver), core/host_providers.py |
| Runtime descriptors | runtime/registry.py |
| User-facing output / diagnostics | CommandLogger / console owner |
| Compiled-output writes (atomic) | CompiledOutputWriter |
| Deployment provenance / state | deployment_ledger.py |
| Install success / failure outcome | the canonical install-outcome path |
| Neutral hook shape -> per-target native | the neutral hook IR + per-target integrators |
| File-level deploy / sync / cleanup | BaseIntegrator (see integrators.instructions.md) |
| Windows stable executable path | install.ps1 ($currentDir / $currentExe) |

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
