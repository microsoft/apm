# Evals: apm-review-panel

Per genesis Step 8 evals gate. Two categories:

1. **TRIGGER EVALS** validate the dispatch description correctly
   discriminates should-trigger queries from near-miss should-NOT
   queries. Validation split is the ship gate (>= 0.5 on positives,
   < 0.5 on negatives).

2. **CONTENT EVALS** validate that the skill, when activated, produces
   the JSON-derived top-loaded verdict comment in the shape declared
   by `assets/verdict-template.md`. Run with-skill vs without-skill;
   if the deltas are not visible, the skill is not adding value.

## Files

- `trigger-evals.json` -- 16 queries (8 should-trigger + 8 should-NOT),
  60/40 train/val split.
- `content-eval-clean-pr.md` -- synthetic clean PR scenario; expected
  verdict = APPROVE, all panelists return `required: []`.
- `content-eval-rejected-pr.md` -- synthetic PR with one architectural
  smell + one nit; expected verdict = REJECT, python-architect returns
  one `required` finding.
- `run-verdict-harness.py` -- DETERMINISTIC harness covering the
  parts of the panel that do NOT require an LLM: JSON schema
  validation (S4 gate) and verdict computation. Five cases: two
  positive (clean-pr APPROVE, rejected-pr REJECT) and three negative
  (missing-nits, unknown-persona, disposition-leak). All five MUST
  pass before merging any change to schemas, the SKILL.md execution
  checklist verdict rule, or the persona output contracts.

## How to run

### Deterministic harness (free, runs anywhere)

```
uv run --with jsonschema python3 \
    .apm/skills/apm-review-panel/evals/run-verdict-harness.py
```

Expected: `RESULT: ALL PASS`. Proves schemas reject malformed shapes
and verdict math is correct. Does NOT prove the LLM panelists will
return well-formed JSON in practice -- that requires a real run.

### Full end-to-end (option B, branch-pin test)

The gh-aw workflow imports the panel skill from `microsoft/apm#main`
for security (anti-self-approval). Pre-merge validation requires a
temporary branch pin:

1. On the feature branch, change `imports.packages` in
   `.github/workflows/pr-review-panel.md` from `microsoft/apm#main`
   to `microsoft/apm#<feature-branch>`.
2. `gh aw compile pr-review-panel`.
3. Commit as `chore: TEMP pin to branch for end-to-end test`.
4. Open a tiny throwaway PR; label it `panel-review`.
5. Observe: 6 task threads spawn, JSON returns, verdict label
   applied, `panel-review` removed, `panel-approved` (or
   `panel-rejected`) set. Push a commit and watch the
   deterministic `pr-panel-label-reset.yml` strip the verdict
   label.
6. Revert the temp-pin commit before merge.

### Trigger evals

Trigger evals can be run via the genesis evals harness or any
dispatcher that loads the skill description and queries it.
