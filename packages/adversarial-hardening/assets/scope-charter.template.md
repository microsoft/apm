# Scope charter - TEMPLATE (the anti-scope-creep spine)

Instantiate this template ONCE per hardening run, BEFORE any lens
fans out. Fill every `<...>` slot, present it to the operator, and do
NOT begin the adversarial loop until the operator RATIFIES it (B10
HUMAN CHECKPOINT). Save the filled copy alongside the findings-ledger
and RELOAD it at the start of every round and every arbiter pass
(B8 ATTENTION ANCHOR, B9 GOAL STEWARD).

The charter is the contract the charter-arbiter gates EVERY finding
against. A finding the charter does not place IN scope is DECLINED --
recorded with the declining clause id in the ledger -- never folded.
This is the structural countermeasure to the goal-drift failure
(a hardening run that grew a third-party secret scanner and consumed
~25 of 32 rounds on out-of-scope work).

---

## 1. Target (the ONE bounded surface)

- Surface: `<PR # / module path / CLI command>`
- Head ref: `<branch or sha being hardened>`
- One-sentence purpose: `<what this surface is responsible for>`

A charter governs ONE surface. If the operator names several, ratify
one charter per surface or narrow to the highest-stakes one.

## 2. IN scope (what THIS target owns -- the only place findings may land)

List the responsibilities the target itself owns. A finding is
ACCEPT-eligible only if its root cause lives here.

- `<e.g. how the runner parses and expands user-supplied env vars>`
- `<e.g. the lockfile writer's atomicity under partial failure>`
- `<...>`

## 3. OUT of scope (shared-responsibility / other-owner clauses)

Each clause gets a STABLE id (`OOS-1`, `OOS-2`, ...). When the arbiter
declines a finding, it records the matching clause id as
`decline_clause` in the ledger, and the dossier surfaces it -- so the
PR shows what was deliberately NOT done and why.

- `OOS-1` Domain detectors are NOT built here. Detecting another
  platform's secrets / malware / licenses by shape is the script
  author's job under shared responsibility. (The canonical scope-creep
  trap: a leaked third-party token in stdout is `OOS-1`, not a finding
  to fix by building a scanner.)
- `OOS-2` OS / TLS / proxy / kernel facilities are NOT reimplemented;
  only how the target USES them is in scope.
- `OOS-3` Issue-queue triage and opening PRs across a backlog are NOT
  done here (that is apm-issue-autopilot / batch-bug-shepherd).
- `OOS-4` `<target-specific out-of-scope clause>`

## 4. Invariants (capabilities a fix may NEVER remove to close a finding)

A fix that would violate an invariant is REJECTED even if it closes a
real finding. Each invariant SHOULD carry a control test the fold must
keep green.

- `INV-1` Corporate-proxy egress is preserved (removing a
  legitimate-use capability to "harden" it is collateral damage, not a
  fix).
- `INV-2` `<other must-not-break legitimate behavior>`

## 5. Pillar invariants (always present -- do NOT delete)

These five are non-negotiable and apply to EVERY run. The
charter-arbiter and the push-hygiene gate enforce them.

- `PILL-A1 PR-CARRIES-GROUNDED-FINDINGS` -- the PR body's
  `## Hardening findings and resolution` section is rendered
  deterministically from the findings-ledger by
  `scripts/render-dossier.py` and embedded VERBATIM by
  pr-description-skill. The findings story is never narrated from LLM
  recall.
- `PILL-B1 TESTS-CI-COLLECTED` -- every new/changed test in a fold is
  COLLECTED by the merge-queue lane CI uses (not merely green in some
  lane). An uncollected test is a push-hygiene REJECT, never folded.
- `PILL-B2 DIFF-MINIMAL-NO-ORPHANS` -- a fold's diff carries no
  orphaned fixtures, scratch/debug files, or scaffolding unreferenced
  by a collected test. Dead artifacts are a REJECT.
- `PILL-B3 RIGHT-ALTITUDE-TESTS` -- a fix crossing module boundaries
  carries an INTEGRATION test, not only a unit test; a localized fix
  carries a focused unit test. A cross-module fold shipping only unit
  coverage is flagged (silent-drift risk).
- `PILL-X1 NO-AUTO-MERGE` -- the protected-branch merge stays a human
  gate. This skill drives to ship_now-ADVISORY and stops.

## 6. Stance and budget

- Stance: `<frugal | balanced | quality>` (default balanced).
- Budget escalation: every `<N>` rounds of only-marginal findings,
  escalate to the operator (B10) rather than auto-continue or
  auto-stop. Termination stays the fixpoint PROPERTY (findings-ledger
  stop-predicate), never a round cap.

---

## Ratification

```
Charter ratified by: <operator>    at: <iso-timestamp>
Target head at ratify: <sha>
```

Until this block is filled by a real human gate, the orchestrator MUST
NOT fan out any lens.
