# docs-corpus-audit evals

Two eval suites, both required for the ship gate.

## trigger-evals.json

20 queries (10 should-trigger + 10 should-NOT-trigger), 60/40 train/val
split. The validation split is the ship gate:

- should-trigger val: >=0.5 must invoke `docs-corpus-audit`.
- should-NOT-trigger val: <0.5 must invoke `docs-corpus-audit` (the
  rest route to `docs-sync`, `doc-writer`, or direct edit).

The boundary with `docs-sync` is the load-bearing distinction:
PR-scope queries -> docs-sync; whole-corpus-scope queries -> here.

## content-evals.json

Three corpus-drift scenarios, each with seeded drift, expected
behavior, and a control baseline (what the LLM does WITHOUT the
skill loaded). The skill must produce a measurably different and
better outcome on each scenario -- if with-skill and without-skill
are indistinguishable, the skill adds no value and should be
redesigned or deleted.

## How to run

These evals are descriptive at present (the run harness is a TODO).
Until the harness lands, treat them as the operator checklist when
authoring or modifying this skill: every change MUST be re-checked
against the val splits manually.
