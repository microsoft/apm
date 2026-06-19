<!--
shepherd-driver - PR-facing comment shapes.

Two blocks. The shepherd-driver subagent renders the PR ADVISORY
COMMENT block when CI is green (exactly ONE per PR per terminal pass).
The SUPERSEDE HANDOFF COMMENT block is rendered when a push fell back
to a superseding PR (status=superseded) and is posted on the ORIGINAL
PR.

These blocks were extracted from batch-bug-shepherd's
final-report-template.md so both batch-bug-shepherd and
apm-issue-autopilot share one PR-comment contract. The orchestrator's
own session-end report (titled per orchestrator) is a SEPARATE
template owned by each orchestrator, not here.

RENDERING RULES:
- ASCII only.
- Skip sections that are empty; do not emit placeholders.
- The PR advisory comment is exactly ONE per PR per terminal pass.
- No verdict labels are applied; this is advisory.
-->

## PR ADVISORY COMMENT block (shepherd-driver -> PR)

Rendered by the apm-review-panel skill when invoked from the
shepherd-driver loop. The shepherd-driver supplies the appended
sections below ("Reservations carried from strategic-alignment",
"Folded in this run", "Copilot signals reviewed", "Deferred",
"Lint", "CI") which the panel comment template incorporates into its
final emission.

{{#if reservations}}
### Reservations carried from strategic-alignment

The parent orchestrator aligned this issue WITH the following
reservations. Each was weighed by the panel this run:

{{#each reservations}}
- {{ summary }}{{#if addressed_by}} -- addressed by {{ addressed_by }}{{/if}}.
{{/each}}
{{/if}}

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

## SUPERSEDE HANDOFF COMMENT block (shepherd-driver -> original PR)

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

Rendered by the conflict-resolution loop (conflict-resolution-prompt.md
step 9) ONLY on `status=resolved`. This is the SECOND-and-final comment
per the two-comment-per-PR cap: the per-PR drive loop posts the PR
ADVISORY comment first; this resolution comment is second. The other
three conflict-resolution statuses (`requires-author-action`,
`requires-human-judgment`, `resolution-failed`) route to the
orchestrator final report WITHOUT a PR comment.

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
