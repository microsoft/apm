<!--
apm-issue-autopilot - final report template (Phase 7).

Rendered ONCE at session end. Mirrors the ground-truth table to the
maintainer. NEVER auto-closes escalated or terminal-by-triage issues;
those are surfaced for human action. ASCII only.
-->

# apm-issue-autopilot - session report

Seed: <list-or-query>. HEAD at seed: `<sha>`. Issues processed: <N>.

## Outcomes

| issue | type | gate | maintainer | outcome | pr | terminal_status |
|-------|------|------|------------|---------|----|-----------------|
{{#each rows}}
| #{{ issue }} | {{ type }} | {{ gate }} | {{ maintainer }} | {{ outcome }} | {{ pr }} | {{ terminal_status }} |
{{/each}}

## Merged-ready PRs

{{#each ready_rows}}
- #{{ pr }} (issue #{{ issue }}, {{ type }}): {{ summary }}. CI green,
  ms=<state>, head `<sha>`. {{ folded_count }} folded,
  {{ deferred_count }} deferred.
{{/each}}

## Advisory / deferred PRs

{{#each advisory_rows}}
- #{{ pr }} (issue #{{ issue }}): ready on stated scope; deferred:
  {{ deferred_list }}.
{{/each}}

## Needs your action (NOT auto-acted)

{{#each escalated_rows}}
- #{{ issue }} ({{ type }}, gate {{ gate }}): {{ reason }}.
  Recommended: {{ recommendation }}.
{{/each}}

## Terminal by triage (no implementation)

{{#each terminal_rows}}
- #{{ issue }}: {{ terminal_status }} -- {{ detail }}.
{{/each}}

## Housekeeping

- Worktrees removed: {{ worktrees_removed }} (branches retained on
  origin for open PRs).
- Labels added by this run and now cleared: {{ labels_cleared }}.
- Labels left in place (pre-existing): {{ labels_preexisting }}.

No issue was closed automatically. Escalated and terminal-by-triage
rows await your decision.
