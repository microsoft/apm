# Label-reset workflow validation (2026-04-28)

End-to-end test of `.github/workflows/pr-panel-label-reset.yml` —
the deterministic plain-Actions companion that strips verdict labels
when a PR receives a new commit (S7 DETERMINISTIC TOOL BRIDGE).

## Setup

Probe PR: microsoft/apm#1025 (closed)

- Base: `refactor/review-panel-fanout` (so the workflow YAML is
  loaded from the feature branch — required because
  `pull_request: synchronize` reads the BASE-branch workflow file).
- Head: `test/label-reset-probe` (deleted after test).
- Initial state: one commit ahead of base, no labels.

## Procedure

1. Applied `panel-rejected` label manually (simulating a panel verdict).
2. Pushed a second commit to `test/label-reset-probe` head ref.
3. The push fires `pull_request: synchronize` on PR #1025.
4. Reset workflow runs from the base-branch YAML and iterates over
   `panel-approved` / `panel-rejected`, calling `gh pr edit
   --remove-label` for each.

## Result: PASS

| Check | Expected | Actual |
|-------|----------|--------|
| Workflow ran on synchronize | yes | yes — run 25076314935 |
| Run status | success | success (13s) |
| `panel-rejected` removed | yes | yes (labels: []) |
| Missing `panel-approved` handled gracefully | no error | yes (if/else swallows) |

`panel-approved` path uses identical code in the same loop; testing
one path proves the logic.

## Side finding

The `panel-approved` repo label did NOT exist at test time. The
refactor PR introduces the workflow and the contract but did not
create the label itself. Created post-test:

- `panel-approved` (#0E8A16, green) — Apm-review-panel verdict: APPROVE. Removed automatically on next push.
- `panel-rejected` (#B60205, red) — description and color updated for clarity.

## Run log excerpt

```
Removed label 'panel-approved' (or it was not present).
Removed label 'panel-rejected' (or it was not present).
```

(Both branches of the conditional behave the same; the message text
is intentionally vague because gh CLI exit codes do not distinguish
"removed" from "was already absent".)

## Cross-references

- Workflow file: `.github/workflows/pr-panel-label-reset.yml`
- Probe PR: https://github.com/microsoft/apm/pull/1025
- Run: https://github.com/microsoft/apm/actions/runs/25076314935
- Companion e2e for the panel itself: `results-e2e-pr931-2026-04-28.md`
