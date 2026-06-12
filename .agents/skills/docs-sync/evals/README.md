# docs-sync evals

This directory holds the eval suite for the `docs-sync` skill, per
the genesis canonical evals doctrine (MODULE ENTRYPOINT primitive).

## Files

- `trigger-evals.json` -- 20 dispatch evals (10 should-trigger,
  10 should-NOT-trigger), 60/40 train/val split. The validation
  split is the ship gate: rate >= 0.5 on should-trigger AND
  < 0.5 on should-not-trigger.

- `content-evals.json` -- 3 content scenarios (E1 surgical CLI
  fix, E2 new flag, E3 new package format) exercised
  with_skill vs without_skill to prove value-delta.

## Ship gates

The skill is ready to graduate from rung 1 (label-gated) to rung 2
(default-on) when ALL of these pass:

1. Trigger-eval val split: rate >= 0.5 on should-trigger AND
   < 0.5 on should-not-trigger.
2. Content evals E1, E2, E3 each produce a measurable value-delta
   between `with_skill` and `without_skill` runs.
3. Shadow-run on >= 5 recent real PRs in microsoft/apm with
   no false-alarm advisories on test-only / CI-only PRs.
4. Cost ceiling (15 LLM calls) not hit on any shadow-run case.

## Notes

- Eval execution is currently manual. Future: tie into a CI job
  similar to `apm-review-panel/evals/render_eval.py`.
- The shadow-run phase is the most important. Synthetic evals
  cannot fully predict classifier accuracy on real PR diffs.
