# Copilot review classification prompt

Consumed by: `assets/shepherd-driver-prompt.md` Phase X.0.

Each shepherd-driver iteration fetches the inline review and
inline comments left by `copilot-pull-request-reviewer[bot]` on the
target PR, then runs this classification template once per item to
produce a `{id, classification, rationale}` triple.

ASCII only.

## Fetch contract

```
gh api repos/microsoft/apm/pulls/<PR>/reviews \
   --jq '[.[] | select(.user.login=="copilot-pull-request-reviewer[bot]")
          | {id, state, submitted_at, body}]'

gh api repos/microsoft/apm/pulls/<PR>/comments \
   --jq '[.[] | select(.user.login=="copilot-pull-request-reviewer[bot]")
          | {id, path, line, body, in_reply_to_id, original_commit_id}]'
```

If no Copilot review exists yet (the bot has not been invoked on
this PR), record `copilot_rounds: 0` in the return and proceed to
the panel step. Do NOT block on Copilot.

## Classification axes

For each Copilot comment, decide ONE of:

- **LEGIT** -- the comment identifies a real defect, gap, or
  ergonomic miss in the diff. Fold per the fold-vs-defer rubric.
  Most LEGIT items will be folded (Copilot inline comments are
  almost always scoped to the diff under review, which makes them
  in-scope by construction).
- **NOT-LEGIT** -- the comment is wrong, irrelevant, hallucinated,
  contradicts established project convention, or proposes a change
  the project has explicitly declined. Record the one-line
  rationale; do NOT silently ignore.

Hard rule: every Copilot item gets a classification entry, even if
NOT-LEGIT. The classification log is visible in the final advisory
comment's "Copilot signals reviewed" section so contributors and the
maintainer can see that the bot was not ignored.

## Per-item template

For each Copilot comment, fill in:

```json
{
  "id": "<github comment id>",
  "path": "<file>",
  "line": <int>,
  "body_excerpt": "<first 200 chars of the bot's comment>",
  "classification": "LEGIT" | "NOT-LEGIT",
  "rationale": "<one line: why this is or is not a real issue>"
}
```

Classification rationale guidance:

- LEGIT items: rationale names the defect concretely. Examples:
  "missing null guard on the new helper", "off-by-one on the new
  loop bound", "new error message lacks the operand it references".
- NOT-LEGIT items: rationale names the reason. Examples:
  "proposed change contradicts the explicit policy of preferring
  composition over inheritance in `apm_cli/integration`", "the
  helper is intentionally untyped per the public API contract",
  "bot misidentified the symbol; the cited function is in a
  different module".

## Round cap

Copilot re-fires on every push, so a new round of Copilot comments
may arrive after each push. Re-fetch after every push WITHIN the
shepherd-driver iteration AND at the top of each new outer
iteration. Hard cap: **2 Copilot rounds per shepherd-driver run**.
After round 2, declare Copilot drained and do NOT re-fetch -- record
`copilot_rounds: 2, copilot_drained: true` in the return.

## What flows where

- LEGIT items: enter the `folded_items` array of the return (the
  fold-vs-defer rubric usually classifies them FOLD since Copilot
  is in-diff by construction). On the rare LEGIT-but-DEFER case
  (e.g. Copilot recommends "while you're here, also refactor X"),
  enter the `deferred_items` array with the scope-boundary note.
- NOT-LEGIT items: enter the `copilot_findings` array with
  `classification: NOT-LEGIT` for the final advisory comment.

The final advisory comment surfaces ONE line per LEGIT-folded item
("resolved in <sha>") and ONE line per NOT-LEGIT item ("reviewed
and declined: <one-line rationale>"). This is the entire on-PR
footprint of the Copilot loop -- no extra inline reply comments.
