<!--
apm-issue-autopilot - ground-truth table (the A11 state interlock).

The orchestrator maintains EXACTLY ONE instance of this table in
plan.md, plus the proceed_manifest block below it. Rewrite the whole
table on every child return. Reload it at the start of every phase and
before/after every spawn. Do not keep parallel state in memory. This
is the single source of truth for the reconciliation loop.

Columns:
- issue: GitHub issue number.
- type: triaged type (type/bug|feature|docs|refactor|performance|
  architecture|automation|release).
- decision: triage decision (accept|needs-design|decline-with-reason|
  duplicate-of|defer-later|auto-handle).
- confidence: high|medium|low.
- gate: auto-proceed|escalate|terminal (from confidence-gate-rubric).
- maintainer: pending|approved|rejected|overridden-to-proceed.
- owner: which thread owns the row now (orchestrator | implement-<n> |
  shepherd-<n>).
- state: phase-stage for this row (see enum below).
- attempt: integer attempt_count for the current owner action.
- pr: PR number once opened.
- author: PR author handle.
- worktree: short slug of the worktree THIS run created for the
  implement/shepherd child (full path is
  copilot-worktrees/awd-cli/<slug>); blank if none. Cleanup removes
  ONLY worktrees recorded here.
- labels_added: comma-joined labels THIS run added (e.g.
  status/shepherding). Cleanup strips ONLY these; pre-existing labels
  are never touched. Blank if the run added none.
- head_sha: full sha captured at PR terminal from the driver
  completion_return. The crash-survivable A11 stop evidence -- do not
  rely on the transient child return alone.
- merge_state: terminal mergeability projection
  mergeable/merge_state_status/ci_status (e.g. MERGEABLE/BLOCKED/green)
  from the completion_return.
- last_verified: short timestamp/sha of last deterministic check.
- terminal_status: set only when the row reaches a terminal state.
- notes: short freeform; child session refs, blocker text.

state enum:
  pending-triage triaged pending-gate gated
  pending-implement implementing pr-opened
  pending-shepherd shepherd-iter-1 shepherd-iter-2 shepherd-iter-3
  shepherd-iter-4 pending-conflict
  ready-to-merge advisory-with-deferred superseded blocked
  escalated-to-maintainer declined duplicate deferred auto-handed-off

Lines stay under 200 chars. ASCII only.

CELL-WRITE RULE (prevents the dogfood table-escaping blowup): write
each cell as a ONE-LINE, sanitized ASCII summary. NEVER backslash-
escape Markdown metacharacters (`|` `_` `*` backtick) -- the renderer
does not need it and the escapes corrupt the table. If a child-return
value contains a literal pipe `|` or a newline, REPLACE it with ` / `
(or a space) before writing the cell. Keep rich/multi-line detail
(blocker paragraphs, full session refs) in the child return or a PR
comment, never inside a table cell.
-->

# Ground-truth table

| issue | type | decision | confidence | gate | maintainer | owner | state | attempt | pr | author | worktree | labels_added | head_sha | merge_state | last_verified | terminal_status | notes |
|-------|------|----------|------------|------|------------|-------|-------|---------|----|--------|----------|--------------|----------|-------------|---------------|-----------------|-------|
| #___ | | | | | pending | orchestrator | pending-triage | 0 | | | | | | | | | seeded from <list-or-query> |

# proceed_manifest

<!--
One row per triaged issue. Phases 3-6 act ONLY on rows where
maintainer_decision is approved or overridden-to-proceed AND gate is
not terminal. Rebuilt from the maintainer's Phase 2 reply.
-->

| issue | gate | maintainer_decision | override_reason | implementation_brief_ref | status |
|-------|------|---------------------|-----------------|--------------------------|--------|
| #___ | | pending | | | awaiting-digest |
