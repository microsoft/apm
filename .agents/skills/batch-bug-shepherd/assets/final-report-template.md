<!--
batch-bug-shepherd - final report AND PR confirmation comment shapes.

Two templates in one file. The orchestrator renders the FINAL REPORT
block at end of session. Completion subagents render the PR
CONFIRMATION COMMENT block when CI is green.

RENDERING RULES:
- ASCII only.
- Skip sections that are empty; do not emit placeholders.
- The PR confirmation comment is exactly ONE per PR per completion
  pass.
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
- PR #{{ pr }} (issue #{{ issue }}, author @{{ author }}) -- {{ iterations }} iter, {{ folded_count }} folded, {{ deferred_count }} deferred, CI {{ ci_evidence_short }}
{{/each}}

### Advisory with deferred items (iteration cap reached)

{{#each advisory_with_deferred}}
- PR #{{ pr }} (issue #{{ issue }}): {{ deferred_count }} item(s) deferred -- see PR advisory comment for scope-boundary rationale.
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

### Disciplines honored this run

- Verify-before-fix: {{ triage_pass_count }} / {{ candidate_count }} verified on HEAD.
- PR-in-flight cross-reference: {{ inflight_count }} community PR(s) shepherded; 0 community PRs duplicated.
- Ownership signaled: {{ assigned_count }} issue(s)/PR(s) assigned + labeled `status/shepherding`; {{ label_removed_count }} label(s) cleared on terminal.
- Fold-by-default discipline: {{ folded_total }} item(s) folded; {{ deferred_total }} deferred (each with scope-boundary note).
- Copilot loop: {{ copilot_legit_total }} LEGIT folded, {{ copilot_declined_total }} NOT-LEGIT reviewed and declined.
- CI verification: {{ ci_green_count }} / {{ pr_count }} PR(s) observed-green; {{ ci_iter_total }} CI fix iteration(s) total.
- Mutation-break gate: {{ mutation_break_count }} regression-trap test(s) verified by guard deletion.
- Lint contract: {{ lint_silent_count }} push(es) gated by silent ruff pair.

### Mergeability status table

Aggregated by the orchestrator at saga-end from every shepherd-
driver completion_return (fields `head_sha`, `mergeable`,
`merge_state_status`, `ci_status` plus `iterations`,
`folded_items.length`, `deferred_items.length`, `copilot_rounds`,
`panel_final_verdict`). One row per shepherded PR. `head_sha`
column is the first 7 chars of the recorded sha.

| PR | head SHA | CEO stance | iters | folds | defers | Copilot rounds | CI | mergeable | mergeStateStatus | notes |
|----|----------|------------|-------|-------|--------|----------------|----|-----------|------------------|-------|
{{#each mergeability_rows}}
| #{{ pr }} | `{{ head_sha_short }}` | {{ ceo_stance }} | {{ iterations }} | {{ folds_count }} | {{ deferrals_count }} | {{ copilot_rounds }} | {{ ci_status }} | {{ mergeable }} | {{ merge_state_status }} | {{ notes }} |
{{/each}}

---

## PR ADVISORY COMMENT block (shepherd-driver -> PR)

Rendered by the apm-review-panel skill when invoked from the
shepherd-driver loop. The shepherd-driver supplies the appended
sections below ("Folded in this run", "Copilot signals reviewed",
"Deferred", "Lint", "CI") which the panel comment template
incorporates into its final emission.

### Folded in this run

{{#each folded_items}}
- ({{ source }}) {{ summary }} -- resolved in {{ resolved_in }}.
{{/each}}

### Copilot signals reviewed

{{#each copilot_findings}}
- `{{ path }}:{{ line }}` -- {{ classification }}: {{ rationale }}{{#if resolved_in}} (resolved in {{ resolved_in }}){{/if}}.
{{/each}}

{{#if deferred_items}}
### Deferred (out-of-scope follow-ups)

These items were surfaced by the panel or Copilot but cross the
stated scope of this PR. Each one suggests a separate follow-up.

{{#each deferred_items}}
- ({{ source }}) {{ summary }} -- scope boundary: {{ scope_boundary_crossed }}{{#if suggested_followup_issue}}; suggested follow-up: {{ suggested_followup_issue }}{{/if}}.
{{/each}}
{{/if}}

{{#if mutation_break_evidence}}
### Regression-trap evidence (mutation-break gate)

{{#each mutation_break_evidence}}
- `{{ test }}` -- deleted `{{ guard_removed }}`; test FAILED as expected; guard restored.
{{/each}}
{{/if}}

### Lint contract

`uv run --extra dev ruff check src/ tests/` and
`uv run --extra dev ruff format --check src/ tests/` both silent.

### CI

{{ ci_evidence }} (after {{ ci_iterations }} CI fix iteration(s)).

### Mergeability status

Captured from `gh pr view {{ pr }} --json
mergeable,mergeStateStatus,statusCheckRollup` immediately after
the last push of this run (shepherd-driver step X.8). The
orchestrator aggregates the same fields across every shepherded
PR into the saga-end mergeability table.

| PR | head SHA | CEO stance | iters | folds | defers | Copilot rounds | CI | mergeable | mergeStateStatus | notes |
|----|----------|------------|-------|-------|--------|----------------|----|-----------|------------------|-------|
| #{{ pr }} | `{{ head_sha_short }}` | {{ panel_final_verdict }} | {{ iterations }} | {{ folded_count }} | {{ deferred_count }} | {{ copilot_rounds }} | {{ ci_status }} | {{ mergeable }} | {{ merge_state_status }} | {{ mergeability_notes }} |

### Convergence

{{ iterations }} outer iteration(s); {{ copilot_rounds }} Copilot
round(s). Final panel verdict: `{{ panel_final_verdict }}`.

{{#if cap_hit}}
NOTE: Outer iteration cap (4) reached. Remaining items are listed
under "Deferred" above with the scope-boundary rationale for each.
{{else}}
Ready for maintainer review.
{{/if}}

---

## SUPERSEDE HANDOFF COMMENT block (completion subagent -> original PR)

Thank you for the original work on this fix. To land it promptly we
have opened a superseding PR (#{{ superseding_pr }}) under
microsoft/apm that preserves your authorship via commit trailers and
resolves the follow-ups surfaced by the apm-review-panel pass.

Closing this PR in favor of #{{ superseding_pr }}. Your contribution
is credited on every cherry-picked commit; the superseding PR's body
links back here. Please do raise concerns on the superseding PR if
the changes diverge from your intent -- we want your sign-off too.
