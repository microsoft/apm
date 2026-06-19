<!--
batch-bug-shepherd - Phase 1.5 strategic-alignment gate procedure.

Load trigger (stated in ../SKILL.md Phase 1.5):
"WHEN ENTERING PHASE 1.5".

This is the binding step-by-step for the orchestrator. The SKILL.md
body holds the contract summary; this file holds the procedure.
Do NOT inline this content into SKILL.md -- the body has no headroom
(see SKILL.md size budget invariant).

ASCII only.
-->

# Strategic-alignment gate (Phase 1.5) -- procedure

## When this runs

After Phase 1 triage returns and BEFORE Phase 2 PR-in-flight
cross-reference. Fan-out cardinality `L` = number of rows with
verdict `LEGIT` from Phase 1. If `L = 0`, render P1.5 as `skipped`
(stroke-dasharray) and pass through to Phase 2.

## Why this gate exists

A LEGIT triage verdict is NECESSARY but not SUFFICIENT for entry to
the shepherd / fix waves. A bug can be reproducible and still be
out of scope per `PRINCIPLES.md`. Without this gate, the saga
spends shepherd / fix / completion / mergeability work on bugs the
project should close rather than fix. The gate makes that
strategic decision once, up front, instead of letting it surface as
"why are we shipping this?" during code review.

## External-dependency probes (BEFORE any spawn)

The gate REQUIRES two host-repo artifacts. If either is missing,
ABORT Phase 1.5 with an operator-actionable error (do NOT route as
aligned; do NOT silently skip the gate; the absence of strategic
grounding is itself a saga-level blocker the operator must fix).

1. `.apm/agents/apm-ceo.agent.md` exists at the host repo root.
   - Probe: filesystem check via the runtime's view tool, OR
     `gh api repos/<owner>/<repo>/contents/.apm/agents/apm-ceo.agent.md`.
   - On miss: ABORT with
     "`apm-ceo` persona file not found. Phase 1.5 requires the
     host repo to define `.apm/agents/apm-ceo.agent.md`. See
     `references/strategic-alignment-gate.md` -> external-dependency
     probes."
2. `PRINCIPLES.md` exists at the host repo root.
   - Probe: filesystem check, OR
     `gh api repos/<owner>/<repo>/contents/PRINCIPLES.md`.
   - On miss: ABORT with
     "`PRINCIPLES.md` not found at host-repo root. Phase 1.5
     cannot cite a principle that does not exist. Author
     `PRINCIPLES.md` (with the project's hard nos as P1..PN
     sections) before re-running bbs. See
     `references/strategic-alignment-gate.md` -> external-dependency
     probes."

`MANIFESTO.md` and `README.md` are ALSO loaded by the spawned
apm-ceo subagent per its own scope file. No separate probe needed.

## Spawn procedure

1. Re-render the progress diagram with `P1` styled `done` and
   `P15` styled `active`. Substitute `L` into the P1.5 label.
2. Print the dispatch table mapping each `ceo-align-<issue>`
   subagent_id to its target issue. Per
   `assets/progress-diagram.md` dispatch-table-requirement.
3. Spawn `L` child threads in parallel using
   `assets/strategic-alignment-prompt.md`. Each spawn receives the
   row's issue number, title, body, and Phase 1 triage summary.

## Schema-validation + retry-once + FAIL-OPEN semantics

Schema-validate every return against `verdict-schema.json`
`strategic_alignment_return` (S4 VALIDATION DECORATOR). On the
FIRST malformed return for a row, re-spawn that subagent ONCE with
a clarifying note quoting the schema field that failed. On the
SECOND malformed return:

- ROUTE the row as `aligned` with a `notes` annotation
  "strategic-gate failed open: subagent malformed x2".
- Do NOT demote on infrastructure failure. Silently demoting a
  legit bug under malformed JSON would hide real defects (truth #3
  OUTPUT IS PROBABILISTIC). Better to let the bug proceed through
  Phase 2 and surface as a maintainer-review item than to drop it.

Same rule applies to the two ABORT cases above: if the operator
chooses to override (e.g. PRINCIPLES.md is being authored in the
same PR as the bbs run), the runtime escape is to manually mark
all rows `aligned` in the ground-truth table and re-enter Phase 2.
The gate must NEVER demote under its own infrastructure failure.

## Routing after returns

For each row, apply the verdict:

- `aligned` -> row stays in saga; status remains `triaged`; proceed
  to Phase 2.
- `aligned-with-reservations` -> row stays in saga; status remains
  `triaged`; capture the `reservations` array in the table's
  `notes` column. Downstream phases (Phase 3 panel, Phase 4
  completion) MUST surface the reservations in their review prose.
- `out-of-scope` -> row DEMOTED. Set status to `triaged-deferred`.
  Capture `cited_principle` + `rationale` in the
  `strategic_rationale` column.
- `wrong-direction` -> row DEMOTED. Same as `out-of-scope` for
  saga routing. The two verdicts differ only in framing (`wrong-
  direction` is a hard-no violation; `out-of-scope` is a soft
  scope-creep call) -- both skip Phase 2/3/4/5.

## Skip routing for demoted rows

A demoted row (`status = triaged-deferred`) MUST be skipped by
Phase 2, Phase 3, Phase 4, and Phase 5. Each phase's loop already
filters on row status; the addition is `triaged-deferred` to the
skip set. The orchestrator MUST verify after each phase boundary
that no demoted row appears in any dispatch table.

ONE exception in Phase 2: the orchestrator still runs a
LIGHTWEIGHT `gh pr list` probe against demoted rows to discover if
a PR is already open for them. This is read-only and does NOT
re-enter the saga -- it only feeds the deferred-PR-comment
procedure below.

## Deferred-PR strategic-rejection comment (S7+A9 subagent)

When a demoted row has `pr_in_flight = true` (discovered by the
Phase 2 lightweight probe), spawn ONE `strategic-reject-<pr>`
subagent. The subagent:

1. PLANS the comment body using `cited_principle` + `rationale` +
   a markdown link to PRINCIPLES.md.
2. EXECUTES via `gh pr comment <pr> --body-file <tmp>`.
3. VERIFIES via `gh pr view <pr> --json comments` that the comment
   landed (S4 ACCEPTANCE OBSERVER).

Comment shape (ASCII; courteous; no jargon):

```
Thank you for the work on this PR.

The batch-bug-shepherd strategic-alignment gate (apm-ceo persona)
reviewed the underlying bug against PRINCIPLES.md and flagged a
direction conflict before we ask the review panel to spend time on
the diff. Reason:

> {{ cited_principle }}

{{ rationale }}

We recommend closing this PR. If you believe the principle does
not apply, please reply here and the maintainer will weigh in.
Full principles: {{ link_to_PRINCIPLES_md }}
```

This comment uses the would-be Phase-4 completion-confirmation
slot under the existing two-comments-per-PR cap. A strategically
demoted PR can never reach Phase 4 (Phase 4 only operates on rows
that completed Phase 3), so the slot is free; no cap violation.

If the strategic-reject comment fails to post (S7 verify reports
missing), record the failure in plan.md with status
`gate-comment-failed`. Do NOT retry; surface to the operator at
Phase 6.

## Phase 6 surfacing

The Phase 6 final report MUST include a `Recommend close as
out-of-scope` partition for demoted rows. See
`assets/final-report-template.md`. The partition cites the
principle and rationale per row so the maintainer can act on each
in one pass.

## Failure modes (operator-readable summary)

| Failure                                  | Routing                                       |
|------------------------------------------|-----------------------------------------------|
| `apm-ceo.agent.md` missing               | ABORT Phase 1.5; operator authors / installs  |
| `PRINCIPLES.md` missing                  | ABORT Phase 1.5; operator authors             |
| Subagent malformed x1                    | Re-spawn ONCE with schema-clarifying note     |
| Subagent malformed x2                    | Route row as `aligned` + notes annotation     |
| Subagent claims a principle that does not exist | Route as `aligned` + notes annotation  |
| Strategic-reject comment fails to post   | Status `gate-comment-failed`; surface in P6   |
