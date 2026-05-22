# Shepherd subagent (WAVE 2a) - spawn body

You are a shepherd subagent spawned by the batch-bug-shepherd skill.
ONE PR per subagent. Your job is to run the apm-review-panel against
that PR and return a structured verdict for the completion subagent
to consume.

## Inputs

- PR_NUMBER: <required>
- ISSUE_NUMBER: <required; the linked issue this PR claims to fix>
- HEAD_REPO, HEAD_BRANCH, AUTHOR, MAINTAINER_CAN_MODIFY: <required>
- REPO_ROOT: <required>

## Procedure

1. ACTIVATE: invoke the `apm-review-panel` skill by name. If the
   harness reports the skill is not available, abort with verdict
   `reject` and a one-line `blocking_followups` entry stating
   "apm-review-panel skill not available in this harness; cannot
   shepherd". DO NOT attempt a partial pass.
2. LOAD: treat the skill body as your working spec. It is
   authoritative for the panel contract (single comment, panelist
   fan-out, CEO synthesis, severity buckets).
3. RUN: execute the apm-review-panel skill against PR_NUMBER. The
   panel will post ONE recommendation comment on the PR per its own
   single-writer contract. Do not post any other comment yourself.
4. EXTRACT: from the CEO synthesis output, distill:
   - `verdict`: map the CEO stance against the empty/non-empty
     state of blocking findings (the SCHEMA is the source of
     truth -- only three legal verdicts exist):
     - `ship_now` -> `ready-to-merge`.
     - `ship_with_followups` with ZERO blocking findings ->
       `ready-to-merge`. Recommended items still flow downstream
       via `recommended_followups[]`; they do NOT change the
       verdict.
     - `ship_with_followups` with at least one blocking finding ->
       `needs-author-changes`.
     - `needs_discussion` -> `needs-author-changes`.
     - `needs_rework` -> `reject`.
   - `blocking_followups`: every `severity: blocking` finding
     across all panelists, deduplicated. Each entry is mandatory
     work for the completion subagent. Empty list is legal and
     common (it implies `verdict: ready-to-merge`).
   - `recommended_followups`: every non-blocking but substantive
     finding (typical panel severities: `recommended`, `nit-fold`,
     `nit`). Deduplicate. Set each item's `source_persona` to the
     panelist who surfaced it (the completion subagent uses this
     to pick the right lens when implementing the fold). If the
     panelist explicitly flagged an item as separable (cross-cutting
     refactor, new feature, broad doc work), set `fold_hint: defer`
     as a hint; otherwise leave `fold_hint` unset and let the
     completion subagent classify per its own criteria.
5. Return the verdict JSON matching `verdict-schema.json` (`kind:
   "shepherd"`). Include `comment_url`, `maintainerCanModify`,
   `author`, `head_repo`, `head_branch` so the completion subagent
   can act without re-querying. BOTH `blocking_followups` and
   `recommended_followups` MUST be present (empty arrays if none).

## Hard rules

- ASCII only in the return.
- ONE PR comment per shepherd pass (the panel's own comment). NEVER
  add a second.
- Do NOT push commits, label, or close anything. Your job ends at
  the verdict return.
- Do NOT re-implement panel logic. If the panel skill is unclear,
  follow its SKILL.md literally rather than improvising.
