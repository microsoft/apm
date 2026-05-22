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
- PR-in-flight cross-reference: {{ inflight_count }} community PR(s) shepherded; 0 community PRs duplicated.
- Mutation-break gate: {{ mutation_break_count }} regression-trap test(s) verified by guard deletion.
- Lint contract: {{ lint_silent_count }} push(es) gated by silent ruff pair.
- Strategic-alignment gate: {{ strategic_gate_count }} LEGIT row(s) inspected by `apm-ceo` against `PRINCIPLES.md`; {{ strategic_aligned_count }} aligned, {{ strategic_aligned_with_reservations_count }} aligned-with-reservations (surfaced downstream), {{ strategic_deferred_count }} demoted (out-of-scope / wrong-direction). Gate failed open on {{ strategic_failed_open_count }} row(s) under infrastructure failure.
- Mergeability gate: {{ gate_run_count }} PR(s) re-probed against current main; {{ resolved_count }} rebased to MERGEABLE; {{ author_action_count }} surfaced to author; {{ human_judgment_count }} escalated to human judgment.
- Two-comment-per-PR cap: at most one completion-confirmation comment + one resolution-confirmation comment.
- Force-push hygiene: every rebase pushed with `--force-with-lease`, never bare `--force`.

---

## PR CONFIRMATION COMMENT block (completion subagent -> PR)

Follow-ups from the apm-review-panel pass have landed. Summary:

{{#each resolved_followups}}
- {{ id }}: {{ summary }} -- resolved in {{ commit_short_sha }}.
{{/each}}

{{#if mutation_break_evidence}}
Regression-trap evidence (mutation-break gate):

{{#each mutation_break_evidence}}
- `{{ test }}` -- deleted `{{ guard_removed }}`; test FAILED as expected; guard restored.
{{/each}}
{{/if}}

Lint contract: `uv run --extra dev ruff check src/ tests/` and
`uv run --extra dev ruff format --check src/ tests/` both silent.

CI: {{ ci_evidence }}

Ready for maintainer review.

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

---

## RESOLUTION CONFIRMATION COMMENT block (conflict-resolution subagent -> PR)

This is the SECOND-and-final comment per the two-comment-per-PR cap.
Rendered only on `status=resolved`; the other three statuses route
to the final report sections (`requires-author-action`,
`requires-human-judgment`, `resolution-failed`) without a comment.

Rebased onto current main at {{ base_sha }} -> {{ new_head_sha }}.

{{#if conflicting_paths}}
Conflicting paths resolved (faithful merge of both intents):

{{#each conflicting_paths}}
- `{{ this }}`
{{/each}}
{{/if}}

{{#if rebase_touched_regression_test}}
Regression-trap test re-verified post-rebase (mutation-break gate):

{{#each mutation_break_evidence}}
- `{{ test }}` -- deleted `{{ guard_removed }}`; test FAILED as expected; guard restored.
{{/each}}
{{/if}}

Lint contract: `uv run --extra dev ruff check src/ tests/` and
`uv run --extra dev ruff format --check src/ tests/` both silent
post-rebase.

Post-push mergeability: `gh pr view --json mergeStateStatus,mergeable`
reports `{{ mergeStateStatus_post }} / MERGEABLE`. Push used
`{{ push_command }}` (--force-with-lease, never bare --force).

Ready for maintainer review.
