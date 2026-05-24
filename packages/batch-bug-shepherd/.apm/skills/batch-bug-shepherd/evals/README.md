# batch-bug-shepherd evals

Two eval families: TRIGGER (does the SKILL.md `description:` fire on
the right queries?) and CONTENT (does the skill output structurally
differ from the no-skill baseline?). The runner is `scripts/run_evals.py`
in the parent skill directory.

## Layout

```
evals/
  evals.json                          # manifest + gates
  triggers.json                       # 10 fire + 10 no_fire, train/val split
  content/
    three-issues-mixed.json           # scenario 1 manifest + rubric
    sweep-bug-queue.json              # scenario 2 manifest + rubric
  fixtures/
    three-issues-mixed.with_skill.md
    three-issues-mixed.without_skill.md
    sweep-bug-queue.with_skill.md
    sweep-bug-queue.without_skill.md
  results/                            # timestamped runner output (gitignored)
```

## Run

From the repo root:

```bash
# val split is the SHIP gate; train split is for tuning the description
python packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd/scripts/run_evals.py
python packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd/scripts/run_evals.py --split train
python packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd/scripts/run_evals.py --filter triggers
python packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd/scripts/run_evals.py --filter content
```

Exit 0 = all gates met. Exit 1 = at least one gate failed. Exit 2 =
runner error (missing manifest, fixture, or malformed JSON).

## Gates

- TRIGGER (val split): >= 0.5 correct-fire rate AND >= 0.5
  correct-no-fire rate.
- CONTENT: per-scenario `with_skill` must hit at least 1 anchor that
  `without_skill` misses (`delta_min_anchors >= 1`).

## Distinguishing from apm-review-panel

The no-fire trigger set deliberately includes queries that SHOULD
route to `apm-review-panel` instead (`review my PR`, `panel-review
this PR`). If the dispatcher confused the two skills, those queries
would mis-fire here and the val-no-fire gate would drop below 0.5.

## Scoring approximation

The runner uses a deterministic keyword/bigram matcher (no LLM) so
the gate is reproducible across machines. See `scripts/run_evals.py`
`score_trigger` for the exact rule. The matcher requires both a
batch-shape anchor (`bug`, `bugs`, `backlog`, `queue`, `prs`,
`issues`) AND at least 3 secondary tokens for a soft-fire; primary
phrases fire on exact match.
