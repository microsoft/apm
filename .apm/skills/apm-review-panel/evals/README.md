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

## How to run

Trigger evals can be run via the genesis evals harness or any
dispatcher that loads the skill description and queries it. Content
evals are run by:

1. Invoke the apm-review-panel skill with the synthetic PR content
   inlined as the panel input.
2. Capture the orchestrator's emitted comment.
3. Diff against the expected output shape declared in the eval file.
4. Note: `with_skill` vs `without_skill` delta is qualitative -- the
   without-skill baseline produces unstructured prose review without
   the binary verdict, fan-out, or label automation. The whole point
   of the skill is the structure.

## Latest run

See PR description for the most recent eval trace.
