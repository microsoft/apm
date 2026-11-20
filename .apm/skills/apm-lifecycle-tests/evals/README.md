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

Score each item as a binary check against the agent transcript, using
tool output in the transcript (pytest runs, grep results), not the
agent's prose claims.

## Results -- run 1 (2026-11-20, skill at PR #4127)

### Trigger evals

Method: one fresh subagent, no tools, given every repo skill name and
description (full text for this skill, condensed for the others) and
the 20 prompts shuffled with fixed seed 4077 and no labels. It
returned the skills it would load per prompt.

| Split | Should-trigger selected | Near-miss selected |
|---|---|---|
| train | 6/6 (1.00) | 0/6 (0.00) |
| validation | 4/4 (1.00) | 0/4 (0.00) |

Ship gate: PASS (validation 1.00 >= 0.5 and 0.00 < 0.5).

Per prompt: every should-trigger prompt selected `apm-lifecycle-tests`.
Near-misses: unit test, property test, linter explanation, lint fix,
CI sharding, startup profiling and test deletion selected no skill;
CHANGELOG update selected `cut-release` + `apm-strategy`; help-text
rename selected `devx-ux`; PR coverage review selected
`autopilot-pr-review-worker`.

### Content evals (C1, C3; C2 not run)

Each arm ran in a fresh subagent with the same prompt. The baseline arm
was told not to read this skill. Edits were limited to one temporary
test file, deleted afterwards.

C1 -- ownership regression:

| `expect_with_skill` item | with skill | without skill |
|---|---|---|
| names promise and cleanup owner | pass (`ownership.preserve_unowned`, owner `file-level-deploy-sync-cleanup`) | fail (none named) |
| installed CLI, hermetic, policy ON | pass | fail (installed CLI, but `audit --ci --no-policy`) |
| sibling bytes + package files gone via before/after snapshot | pass (`ArtifactSnapshotSet` write-set check + byte compare) | fail (file checks only, no snapshot) |
| I1 lock == deployed after uninstall | pass | partial (lock package names only) |
| failing-before and passing-after runs | partial: green on current code because the premise did not reproduce (the user file was preserved in every case), reported honestly, with a negative control (`1 failed`) | partial: red, but on an exit-code expectation, not on the user file |
| bug-ledger row | partial: row drafted, held back without an issue id | fail (none) |

Baseline risks seen: `--no-policy` copied from helpers (yes); CliRunner
or mocked filesystem (no); only a log string asserted (no).
With skill also: I2/I5/I6/I9 oracles, per-target parameters derived
from `KNOWN_TARGETS`, the printed hint executed, and the evidence row.

C3 -- unit test out of scope:

| Item | with skill | without skill |
|---|---|---|
| declines the lifecycle procedure; unit test at the owner | pass (cites "Not for pure unit tests"; `pytestmark = pytest.mark.unit` in `tests/unit/registry/`) | same behavior |
| no hermetic world, no ledger row, no lifecycle markers | pass | pass |

Delta verdict: C1 shows a clear delta (policy ON, snapshot and
invariant oracles, named promise and owner, negative control, hint
executed versus none of these in the baseline). C3 shows no
over-triggering: same unit-scope behavior with and without the skill,
which is the pass condition for that scenario.
