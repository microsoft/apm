<!--
batch-bug-shepherd - final report shape (orchestrator -> user).

The orchestrator renders this FINAL REPORT block once at end of
session. PR-side comments (advisory, supersede handoff, resolution
confirmation) are NOT rendered here -- they are owned by the composed
shepherd-driver skill (see ../shepherd-driver/assets/pr-comment-templates.md).
The BBS orchestrator never posts to a PR directly.

RENDERING RULES:
- ASCII only.
- Skip sections that are empty; do not emit placeholders.
- The final report is exactly ONE per orchestrator session.
- No verdict labels are applied; this is advisory.
-->

## FINAL REPORT block (orchestrator -> user)

# Batch bug shepherd - session report

Scope: {{ scope_description }} ({{ candidate_count }} candidates)

### Ground-truth table

{{ render_table_from_plan_md }}

### Ready to merge

{{#each ready_to_merge_prs}}
- PR #{{ pr }} (issue #{{ issue }}, author @{{ author }}) -- {{ status_note }}; verified MERGEABLE against current main at {{ probe_sha }}.
{{/each}}

### Requires author action (cannot push to fork)

{{#each requires_author_action}}
- PR #{{ pr }} (author @{{ author }}, fork {{ fork_url }}): rebase-needed but `maintainerCanModify=false`. Author intervention required.
{{/each}}

### Requires human judgment (rebase irrecoverable)

{{#each requires_human_judgment}}
- PR #{{ pr }} (issue #{{ issue }}): rebase aborted on {{ conflicting_paths }}; PR intent contradicts main's evolution. Reason: {{ blocker }}.
{{/each}}

### Resolution failed

{{#each resolution_failed}}
- PR #{{ pr }}: conflict-resolution subagent could not complete. Reason: {{ blocker }}.
{{/each}}

### Superseded

{{#each superseded}}
- PR #{{ original_pr }} -> superseded by #{{ superseding_pr }} (author @{{ author }} credited via commit trailers)
{{/each}}

### Blocked (human attention)

{{#each blocked}}
- PR #{{ pr }} (issue #{{ issue }}): {{ blocker }}
{{/each}}

### Unclear triage (human attention)

{{#each unclear}}
- Issue #{{ issue }}: {{ summary }}
{{/each}}

### Closed without fix

{{#each closed_no_fix}}
- Issue #{{ issue }} -- {{ verdict }} ({{ evidence }})
{{/each}}

### Recommend close as out-of-scope (strategic-alignment gate)

Per Phase 1.5 (`apm-ceo` persona vs `PRINCIPLES.md`), the following
rows are LEGIT defects but conflict with the project's direction.
The maintainer should close each (and any associated PR) with a
courtesy comment citing the principle below.

{{#each strategic_deferred}}
- Issue #{{ issue }}{{#if pr}} (PR #{{ pr }}, author @{{ author }}){{/if}} -- verdict `{{ verdict }}` per `{{ cited_principle }}`. {{ rationale }}{{#if comment_landed}} Courtesy comment posted on PR #{{ pr }}.{{/if}}{{#if comment_failed}} (gate-comment-failed: {{ comment_failure_reason }}){{/if}}
{{/each}}

### Aligned with reservations (downstream-surfaced)

{{#each strategic_aligned_with_reservations}}
- Issue #{{ issue }}{{#if pr}} (PR #{{ pr }}){{/if}} -- aligned per `{{ cited_principle }}` with reservations: {{ reservations_joined }}.
{{/each}}

### Disciplines honored this run

- Verify-before-fix: {{ triage_pass_count }} / {{ candidate_count }} verified on HEAD.
- PR-in-flight cross-reference: {{ inflight_count }} community PR(s) driven; 0 community PRs duplicated.
- Mutation-break gate: {{ mutation_break_count }} regression-trap test(s) verified by guard deletion.
- Canonical-owner gate: {{ architecture_gate_count }} PR(s) classified against architecture.instructions.md; {{ dual_guardrail_count }} authority-affecting fix(es) proved both guardrails (behavioral + static + architecture assertion + mutation-break); {{ architecture_blocked_count }} blocked for missing owner evidence.
- Lint contract: {{ lint_silent_count }} push(es) gated by silent ruff pair.
- Strategic-alignment gate: {{ strategic_gate_count }} LEGIT row(s) inspected by `apm-ceo` against `PRINCIPLES.md`; {{ strategic_aligned_count }} aligned, {{ strategic_aligned_with_reservations_count }} aligned-with-reservations (surfaced downstream), {{ strategic_deferred_count }} demoted (out-of-scope / wrong-direction). Gate failed open on {{ strategic_failed_open_count }} row(s) under infrastructure failure.
- Mergeability gate: {{ gate_run_count }} PR(s) re-probed against current main; {{ resolved_count }} rebased to MERGEABLE; {{ author_action_count }} surfaced to author; {{ human_judgment_count }} escalated to human judgment.
- Two-comment-per-PR cap: at most one shepherd-driver advisory comment + one resolution-confirmation comment (both posted by shepherd-driver, not the orchestrator).
- Force-push hygiene: every rebase pushed with `--force-with-lease`, never bare `--force`.
