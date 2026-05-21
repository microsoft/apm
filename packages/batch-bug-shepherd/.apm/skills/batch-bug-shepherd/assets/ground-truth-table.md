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
- status: workflow stage for this row. Allowed values:
    pending-triage
    triaged
    pending-xref
    pending-shepherd
    pending-fix
    pending-completion
    ready-to-merge
    superseded
    blocked
    closed-fixed-at-head
    closed-not-a-bug
- notes: short freeform; cite session refs, commit shas, blocker text.

Lines stay under 200 chars to remain readable in narrow terminals.
ASCII only.
-->

# Ground-truth table

| issue | verdict | pr | pr_in_flight | author | status | notes |
|-------|---------|----|--------------|--------|--------|-------|
| #___ | pending-triage | | | | pending-triage | seeded from <list-or-sweep>; awaiting wave 1 |
