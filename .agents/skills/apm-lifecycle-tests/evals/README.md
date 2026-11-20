# apm-lifecycle-tests evals

Eval suite for the `apm-lifecycle-tests` skill, following the same
convention as `.apm/skills/docs-sync/evals/`.

## Files

- `trigger-evals.json` -- 20 dispatch evals (10 should-trigger,
  10 near-miss should-NOT-trigger), 60/40 train/val split. The
  validation split is the ship gate: rate >= 0.5 on should-trigger
  AND < 0.5 on should-not-trigger.
- `content-evals.json` -- 3 content scenarios exercised
  `with_skill` vs `without_skill` to show value delta:
  - C1 fix: uninstall preserves an unmanaged sibling file next to a
    deployed skill.
  - C2 migrate: an old `*_lifecycle.py` whose only assertion is a
    stdout substring.
  - C3 decline: "add a unit test for a parser" is unit scope.

## Ship gates

1. Trigger-eval val split meets the rates above.
2. C1, C2 and C3 each show a measurable delta between runs: every
   `must` item present with the skill, and at least one missing or a
   `must_not` item present without it.
3. One real task (a lifecycle test for an accepted issue) run with the
   skill, its report block captured, and the skill revised from what
   actually happened.

## Notes

- Execution is manual today. Score each `must` / `must_not` item as a
  binary check against the agent transcript.
- Content checks rely on tool output in the transcript (pytest runs,
  grep results), not on the agent's prose claims.
