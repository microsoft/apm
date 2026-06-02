<!--
batch-bug-shepherd - ground-truth table template

The orchestrator maintains EXACTLY ONE instance of this table in
plan.md. Rewrite the whole table on every subagent return. Reload it
at the start of every wave. Do not keep parallel state in memory.

Columns:
- issue: GitHub issue number (integer, `#` prefix optional).
- verdict: triage outcome (LEGIT | UNCLEAR | FIXED-AT-HEAD | NOT-A-BUG).
- pr: PR number once known (in-flight community PR or own-fix PR).
- pr_in_flight: yes | no | n/a (n/a until cross-reference done).
- author: GitHub handle of the PR author (community contributor or
  internal).
- worktree: slug/path of the git worktree provisioned for this row's
  fix or drive child (Worktree-isolation invariant). Empty for
  read-only / not-yet-provisioned rows; Phase 6 tears down only the
  worktrees recorded here.
- status: workflow stage for this row. Allowed values:
    pending-triage
    triaged
    triaged-deferred
    pending-xref
    pending-shepherd
    pending-fix
    pending-completion
    ready-to-merge
    superseded
    blocked
    closed-fixed-at-head
    closed-not-a-bug
- strategic_verdict: Phase 1.5 verdict (aligned | aligned-with-reservations
  | out-of-scope | wrong-direction | n/a). `n/a` for rows that never
  reached Phase 1.5 (UNCLEAR / FIXED-AT-HEAD / NOT-A-BUG triage verdicts).
- strategic_rationale: short text (<= 200 chars). For demoted rows:
  `<cited_principle>: <one-line rationale>`. For aligned-with-reservations
  rows: `<cited_principle>; reservations: <semicolon-joined list>`.
  Empty for plain `aligned` and `n/a`.
- notes: short freeform; cite session refs, commit shas, blocker text.

Lines stay under 200 chars to remain readable in narrow terminals.
ASCII only.

CELL-WRITE RULE: write each cell as a ONE-LINE, sanitized ASCII
summary. NEVER backslash-escape Markdown metacharacters (`|` `_` `*`
backtick) -- it corrupts the rendered table. If a value contains a
literal pipe `|` or a newline, REPLACE it with ` / ` (or a space)
before writing the cell. Keep rich/multi-line detail (blocker text,
session refs) in the subagent return or a PR comment, never in a cell.
-->

# Ground-truth table

| issue | verdict | pr | pr_in_flight | author | worktree | status | strategic_verdict | strategic_rationale | notes |
|-------|---------|----|--------------|--------|----------|--------|-------------------|---------------------|-------|
| #___ |  | | | | | pending-triage | n/a | | seeded from <list-or-sweep>; awaiting wave 1 |
