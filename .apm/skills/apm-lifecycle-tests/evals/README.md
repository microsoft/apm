# apm-lifecycle-tests evals

Same convention as `.apm/skills/docs-sync/evals/`.

- `trigger-evals.json`: 20 dispatch-description evals, 10
  should-trigger and 10 near-miss should-not-trigger, split 60/40
  (train 6 + 6, validation 4 + 4). Ship gate: validation split rate
  >= 0.5 on should-trigger AND < 0.5 on should-not-trigger.
- `content-evals.json`: 3 scenarios (C1 ownership regression, C2
  migrate a string-only lifecycle file, C3 unit test out of scope).
  Run each with and without the skill; the with-skill output must meet
  every `expect_with_skill` item, and the delta must be visible.

Status: not executed yet. Execution is manual; score each item as a
binary check against the agent transcript, using tool output in the
transcript (pytest runs, grep results), not the agent's prose claims.
Record results here when run.
