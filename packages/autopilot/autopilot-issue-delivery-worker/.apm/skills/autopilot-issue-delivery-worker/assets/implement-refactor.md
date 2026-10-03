# Implement lens: type/refactor and type/performance

Coverage gate: BEHAVIOR-PRESERVING PROOF (+ BENCHMARK for performance).

## type/refactor

1. Characterize CURRENT behavior with tests BEFORE touching structure.
   If the affected paths already have coverage, run it green and note
   it; if not, add characterization tests that pin the existing
   behavior FIRST.
2. Refactor to the brief's `deliverable`. Behavior must not change.
3. Run the full suite green (`uv run --extra dev pytest -q`). The
   characterization tests are unchanged -- if any assertion had to
   change to pass, the refactor altered behavior; that is a scope
   violation, STOP and return `status: escalate`.
4. Run the duplication guard the lint contract specifies (pylint
   R0801) since refactors commonly shift duplicate blocks. Record the
   green suite + unchanged characterization tests as `coverage_gate`.

## type/performance

All of the above, PLUS:

5. Capture a BEFORE measurement on HEAD and an AFTER measurement on
   your branch for the path the brief targets, using the same input.
   Report both numbers in the PR body and as `coverage_gate`. A change
   that does not measurably improve the targeted metric is not a perf
   fix -- return `status: escalate` rather than shipping an unproven
   optimization.

Scope fence: preserve the public contract. A refactor that "while we
are here" changes behavior, signatures, or output format is a
`non_goals` violation.
