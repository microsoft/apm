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
- PR #{{ pr }} (issue #{{ issue }}, author @{{ author }}) -- {{ status_note }}
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
- Mutation-break gate: {{ mutation_break_count }} regression-trap test(s) verified by guard deletion.
- Lint contract: {{ lint_silent_count }} push(es) gated by silent ruff pair.

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
