# release-acceptance evals

Same convention as `.apm/skills/docs-sync/evals/` and
`.apm/skills/apm-lifecycle-tests/evals/`.

- `trigger-evals.json`: 20 dispatch-description evals, 10
  should-trigger and 10 near-miss should-not-trigger, split 60/40
  (train 6 + 6, validation 4 + 4). Ship gate: validation split rate
  >= 0.5 on should-trigger AND < 0.5 on should-not-trigger.
- `content-evals.json`: 4 scenarios (C1 seeded hint regression, C2
  clean candidate with no false blocker, C3 recheck on a moved head, C4
  catalogue unavailable). Run each with and without the skill; the
  with-skill transcript must meet every `expect_with_skill` item.

The deterministic scripts have unit tests in
`tests/unit/test_release_acceptance_scripts.py`.

Execution is manual. Score each item as a binary check against tool
output in the transcript (script JSON, exit codes, files), not the
agent's prose. Record results below when run.

## Results

Not executed yet. The first real run is the post-merge `recheck` on the
0.37 release PR (scope record #4134, done-when (f)).
