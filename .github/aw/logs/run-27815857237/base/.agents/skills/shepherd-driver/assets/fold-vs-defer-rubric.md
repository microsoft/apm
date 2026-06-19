# fold-vs-defer rubric

Consumed by: `assets/shepherd-driver-prompt.md`,
`assets/fix-prompt.md`, `SKILL.md` Phase 4.

The shepherd-driver subagent uses this rubric to decide, for each
follow-up surfaced by the apm-review-panel CEO and each LEGIT
Copilot inline comment, whether to FOLD it into THIS PR or DEFER it
to a separate follow-up issue.

ASCII only.

## The reframe

The old default was "post advisory; address blocking-severity later
in a completion phase". That model treats follow-ups as a backlog to
be drained over time. In practice it leaves PRs landing with known
shortfalls that the next contributor inherits.

The new default is FOLD. The PR lands in the best shape it can
without changing what the PR is ABOUT.

## The decision axis

The axis is NOT severity. The axis is NOT separability. The axis is
SCOPE-CREEP RISK relative to THIS PR's stated scope.

> Stated scope = the one-sentence description in the PR title and
> the first paragraph of the PR body. If the PR body is vague, fall
> back to the linked issue's title.

### FOLD when the follow-up raises the quality bar of the stated scope

Examples that MUST be folded (this is not exhaustive):

- Missing tests for behavior THIS PR introduces.
- CHANGELOG entry for THIS change.
- Documentation drift caused BY this change (a doc page that now
  contradicts the new code surface).
- Warning/error ergonomics on the new surface (the new flag's help
  text, the new error message wording).
- Security hardening on the new code path (input validation on a
  newly-added function; token handling on a newly-added auth path).
- Naming consistency on the new symbols (the helper this PR
  introduces should match the canonical sibling naming).
- Mutation-break gate on a regression test this PR added.
- Lint failures on touched files.

### DEFER only when the follow-up introduces a new theme or domain unrelated to the stated scope

Examples that legitimately defer:

- PR fixes a Windows shim bug; reviewer recommends a wholesale
  CommandLogger refactor across the codebase.
- PR adds opencode runtime validation; reviewer recommends a
  multi-target plugin redesign.
- PR fixes one auth resolver edge case; reviewer recommends migrating
  the entire AuthContext to an async API.
- PR fixes a typo in CHANGELOG; reviewer recommends restructuring the
  release-notes pipeline.

Every deferral MUST be accompanied by a one-line `scope_boundary_
crossed` note naming the boundary. Example:

```
deferred_items:
  - source: panel
    summary: "Refactor CommandLogger to a strategy pattern"
    scope_boundary_crossed: "PR scope is one Windows shim bug; this
      proposes a cross-cutting logger redesign affecting 12 modules."
    suggested_followup_issue: "open a new issue tagged refactor"
```

## Anti-reasons for deferral (NEVER use these)

- "We have limited subagent capacity." False. Capacity is unlimited.
- "This is just a recommended item, not blocking." Severity is not
  the axis.
- "The reviewer can address this in a follow-up PR." Not a reason;
  the WHOLE POINT of this rubric is to not push the cost forward.
- "It's a nit." Nits that align with stated scope still get folded
  (the cost is seconds).
- "The PR is already big." If the in-scope follow-up is small, the
  PR being big does not change the fold decision.

## Loop semantics

The shepherd-driver iterates:

```
fetch Copilot + run panel
  -> apply rubric: classify each item FOLD or DEFER
  -> edit code, fold all FOLDABLE items
  -> lint + push
  -> CI watch (recovery loop, max 3 fix iterations)
  -> re-fetch Copilot (new comments may have arrived on the diff)
  -> re-run panel
  -> repeat until: CEO returns ship_now AND zero foldable items
                   AND zero new Copilot LEGIT items
                   AND CI is green
```

Hard cap: 4 outer iterations per PR. On cap hit, post the final
advisory comment with an explicit "Remaining items + deferral
rationale" section that names every still-unfolded item and the
reason it could not be folded in this run; remove
`status/shepherding` label; record `status=advisory-with-deferred`
in plan.md.

## Quick decision tree

```
Is the item already addressed in the latest commit?
  yes -> mark resolved; do not re-do.
  no -> continue.

Does the item touch code or docs that THIS PR's diff already
modifies, or extend the contract THIS PR introduces?
  yes -> FOLD.
  no  -> continue.

Does the item raise the quality bar of the stated scope
(tests, changelog, doc drift, warning ergonomics, security
hardening, naming) for THIS PR's surface?
  yes -> FOLD.
  no  -> continue.

Does the item introduce a new theme or domain unrelated to the
stated scope?
  yes -> DEFER. Write the one-line scope_boundary_crossed note.
  no  -> FOLD (default).
```

When in doubt: FOLD. The cost of an extra small fold is bounded; the
cost of a missed fold compounds over future PRs.
