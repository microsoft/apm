# apm-review-panel evals

Two complementary evals live here.

## 1. `render_eval.py` (content / output-shape eval)

Renders fixture JSON against the rendering rules of
`assets/recommendation-template.md`. The script is a SPECIFICATION
TEST -- it implements the same rendering rules a panel orchestrator
LLM applies in production, so we can eyeball the output offline
without spending a panel run.

Run:

```bash
python3 render_eval.py
```

Outputs `<fixture>.rendered.md` next to each fixture in `fixtures/`
and prints a summary line per scenario including ASCII-only lint
(per repo encoding rule).

### Fixtures

- `01-ship-now-pr1084-shape.json` -- PR #1084 shape: surgical
  bug-fix, all panelists APPROVE with at most polish nits, CEO
  recommends `ship_now`. Verifies the COMMON case (most PRs) is
  short, scannable, and doesn't bury the lede.
- `02-needs-rework-shape.json` -- PR with two correctness
  regressions (path-traversal + Windows-encoding) + an architecture
  smell. CEO recommends `needs_rework` with explicit blocking-
  severity tags on the top follow-ups. Verifies the panel can be
  HONEST about high-signal feedback without reverting to a binary
  gate.

### What "passing" looks like

A maintainer scanning the rendered output for ~30 seconds gets:
- the stance pill (top of comment),
- the headline + 2-4 paragraph CEO synthesis,
- the per-persona summary table (one row each),
- the top-N curated follow-ups,
- and, where supplied, the architecture diagrams.

Full per-persona findings live inside `<details>`. Open them when
you want depth, ignore them when you don't.

### Adding a fixture

Drop `<NN>-<scenario>-shape.json` into `fixtures/`. Schema follows
`assets/panelist-return-schema.json` (under `panelists[]`) and
`assets/ceo-return-schema.json` (under `ceo`). Re-run
`python3 render_eval.py` and inspect the new `.rendered.md`.

## 2. `trigger-evals.json` (dispatch description eval)

8 should-trigger + 8 should-NOT-trigger queries split 60/40
train/val. The validation split is the ship gate per the genesis
MODULE ENTRYPOINT spec: rate >= 0.5 on should-trigger AND < 0.5 on
should-NOT-trigger.

This is a manual eval against the dispatch description in
`SKILL.md`'s frontmatter -- run by reading the description as if
you were the harness's dispatcher LLM and classifying each query.
