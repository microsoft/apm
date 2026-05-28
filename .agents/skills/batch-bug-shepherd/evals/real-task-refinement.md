# Real-task refinement: batch-bug-shepherd recommendation-fold refactor

Genesis Step 8 mandates that after structural lint passes, the
skill is run on at least one real task, the trace captured, and the
SKILL.md revised from what actually happened (not what was
expected). One-shot drafts that never met execution are not done.

This artifact captures the wave-1 -> wave-2 refinement that drove
the v2 SKILL.md edits.

## Setup

The pre-refactor SKILL.md (v1) ran across the open bug-flagged PR
queue in microsoft/apm. Seven PRs were shepherded in wave-1. The
load-bearing observation: nearly every shepherd-driver terminal
return was `ship_with_followups` with the recommendations posted as
a generic advisory comment and routed to the maintainer's backlog.
This is the recommendations-as-backlog anti-pattern that
`assets/fold-vs-defer-rubric.md` was rewritten to eliminate.

After the v2 SKILL.md rewrite (fold-by-default, Copilot
classification first-class, CI watch + recovery first-class), the
same PRs (plus one new community PR, totaling 7) were re-run as
wave-2 with the new shepherd-driver-prompt.

## Wave-1 vs Wave-2 comparison

PRs in scope: 7 community + own-fix PRs across the bug queue
(numbers redacted from this artifact; full trace lives in the
wave-2 session memo).

| Metric                                         | Wave-1 (v1 SKILL.md)        | Wave-2 (v2 SKILL.md)     |
|------------------------------------------------|------------------------------|---------------------------|
| Per-PR follow-up deferrals (median)            | 6 / 7                        | 0 - 1 / 7                 |
| Per-PR follow-ups folded into this PR (median) | 0 - 1                        | 5 - 11                    |
| Terminal status: ship_with_followups           | 6 / 7                        | 0 / 7                     |
| Terminal status: ship_now / ready-to-merge     | 1 / 7                        | 6 / 7                     |
| Terminal status: blocked                       | 0 / 7                        | 1 / 7 (CI cap hit)        |
| Copilot inline comments classified             | 0 / 7 PRs                    | 7 / 7 PRs                 |
| CI recovery iterations triggered               | 0 (CI ignored)               | 4 (across 3 PRs)          |
| Outer iterations per PR (median)               | 1 (single panel pass)        | 2 (panel + fold + repanel)|

The shape change is the load-bearing one. In wave-1 the shepherd-
driver effectively functioned as a panel-comment poster; in
wave-2 the shepherd-driver functions as a convergence loop that
holds the PR until it can be shipped without known shortfalls.

## Positive trace: iter-4 #1513 CI-infra rollback

PR #1513 (own-fix, addresses #1502 dependency-confusion edge case)
provides a concrete positive trace of the new CI-recovery checklist
firing correctly.

Sequence:

1. Iteration 4 push lands. `gh pr checks 1513 --watch` settles on
   ANY FAIL.
2. Failing job: `Install (Windows)`. log-failed inspection shows a
   `pwsh` invocation 500 from the package source -- transient
   network failure, not a code defect.
3. Classified into bucket 3 (CI infra hiccup) per
   `assets/ci-recovery-checklist.md`.
4. Recovery action: ONE `gh run rerun --failed <id>` per the
   bucket-3 rule (no code commit; the checklist forbids
   speculative code-side fixes on bucket-3 symptoms).
5. ci_iterations advanced 1 -> 2 of cap 3.
6. Re-watch settles ALL GREEN.
7. Phase X.7 promotes to terminal `ready-to-merge`.

Why this matters: the v1 prompt did not distinguish bucket 3 from
bucket 1, so a transient infra hiccup would either be left red (PR
abandoned) or get a speculative code-side push that ate CI budget
without addressing the cause. v2 catches the bucket-3 signature and
applies the cheap recovery first.

## What changed in SKILL.md as a result

The wave-1 trace surfaced three concrete edits that landed in v2:

1. `Default is fold. Defer is the exception.` -- promoted from
   buried sub-bullet in v1 to top of the Architecture invariants
   block. v1 placement let subagents miss it under context
   pressure.

2. `Copilot review is a first-class signal.` -- added as Phase
   X.0 explicitly with its own classification asset. v1 left
   Copilot handling unspecified; in practice the bot signal was
   dropped.

3. `CI must be observed green.` -- added as Phase X.6 with the
   four-bucket recovery checklist. v1 stopped at push.

The verdict-schema and final-report templates were updated to
carry the new metrics (folded_count, deferred_with_scope_boundary_
crossed, copilot_rounds, ci_iterations, ci_bucket_history).

## Evals coverage of the refinement

Three structured-input content evals (added in the genesis Step 6
backfill) exercise the load-bearing decision policies surfaced by
this real-task refinement:

- `content/fold-vs-defer-panel.json` -- exercises Phase X.2 fold-
  vs-defer-rubric application; mirrors the wave-1 anti-pattern in
  the `without_skill` fixture and the wave-2 corrected behavior in
  the `with_skill` fixture.
- `content/copilot-classification-and-fold.json` -- exercises Phase
  X.0 Copilot classification; mirrors the wave-1 bot-signal-drop
  anti-pattern.
- `content/ci-recovery-lint-bucket.json` -- exercises Phase X.6
  bucket-1 lint-recovery; the iter-4 #1513 trace above is the
  bucket-3 sibling that is documented but not yet codified as its
  own scenario (future expansion).

## Caveat on evaluation methodology

This skill takes 30+ minutes per real-PR shepherd-driver run, and
the panel + Copilot + CI assets it composes against are network-
gated. A true `with_skill` vs `without_skill` comparison on live
PRs is infeasible at CI cadence. The structured-input evals exercise
the load-bearing decision policy (fold-vs-defer, classification, CI
bucket routing) rather than the long-running orchestration. The
wave-1 -> wave-2 comparison above stands as the
real-task-refinement evidence; the structured-input evals stand as
the per-change regression guard.
