<!--
apm-issue-autopilot - consolidated triage digest (the ONE review).

The orchestrator renders this digest EXACTLY ONCE per session, after
all triage children return and the confidence gate has been applied.
It is the single human checkpoint: the maintainer reads it and marks
each row's maintainer_decision. Do NOT render per-issue mini-reviews.

RENDERING RULES:
- ASCII only.
- One row per issue, ALL issues in one table.
- Auto-proceed rows additionally render their implementation brief
  below the table so the maintainer can sanity-check scope.
- Sort: escalate rows first (they need attention), then auto-proceed,
  then terminal.
-->

# Triage digest - <N> issues - <date>

Seed source: <list-or-query>. HEAD: `<sha>`. One consolidated review;
mark each row, then I proceed only on approved / overridden-to-proceed
rows. Escalate is the default; auto-proceed is the exception.

## Decisions

| issue | title | type | decision | confidence | gate | red flags | recommended next |
|-------|-------|------|----------|------------|------|-----------|------------------|
{{#each rows}}
| #{{ issue }} | {{ title }} | {{ type }} | {{ decision }} | {{ confidence }} | {{ gate }} | {{ red_flags }} | {{ next_action }} |
{{/each}}

## Implementation briefs (auto-proceed rows only)

{{#each auto_proceed_rows}}
### #{{ issue }} - {{ title }}

- deliverable: {{ deliverable }}
- non-goals: {{ non_goals }}
- acceptance tests: {{ acceptance_tests }}
- docs required: {{ docs_required }}
- risk surface: {{ risk_surface }}

{{/each}}

## Escalated rows (need your call)

{{#each escalate_rows}}
- #{{ issue }} ({{ type }}, confidence {{ confidence }}): {{ decision_detail }}
  Red flags: {{ red_flags }}. Reason escalated: {{ escalation_reason }}.
{{/each}}

## Your decision

For each row, reply with one of: `approved`, `rejected`, or
`overridden: <reason>`. Unmarked rows default to `rejected` (no action
taken). I will not write any code or labels until you respond.
