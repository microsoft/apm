---
name: apm-issue-autopilot
description: >-
  Use this skill to drive any open microsoft/apm issue (bug, feature,
  docs, refactor, perf) from raw intake to a mergeable PR with triage
  as the central, paramount gate. Run the apm-triage-panel rubric per
  issue first, then present ONE consolidated triage review for the
  whole batch and escalate to the maintainer BY DEFAULT on any doubt
  (needs-design, decline, duplicate, defer, auto-handle, breaking-
  change, auth/security/governance surface, low arbiter confidence,
  unbounded scope, or a missing brief); only auto-implement clear,
  bounded, high-confidence accepts the maintainer
  approved. Then drive each accepted PR to mergeability batch-bug-
  shepherd style via the shepherd-driver loop: fold copilot + panel
  follow-ups by default, watch CI green, iterate under a bounded cap.
  Invoke MANUALLY, in-session, on an issue list or queue -- never by
  label or event. Activate when the maintainer asks to auto-tackle the
  issue queue, clear the backlog to PRs, or run issues to merge --
  even if "autopilot" is not named.
---

# apm-issue-autopilot - intake-to-merge issue orchestrator

This manually invoked A11 RECONCILIATION LOOP extends
[batch-bug-shepherd](../batch-bug-shepherd/SKILL.md) to all issue types.
It composes the triage rubric and per-PR convergence loop below rather
than re-implementing them. Refactors follow the persisted genesis design.

## What it composes (do not re-implement)

- [apm-triage-panel](../apm-triage-panel/SKILL.md) -- the triage
  rubric, run per issue in DIRECT (orchestrator-return) mode.
- [shepherd-driver](../shepherd-driver/SKILL.md) -- the per-PR drive-
  to-merge convergence loop and cross-PR mergeability gate (which in
  turn composes apm-review-panel).
- [pr-description-skill](../pr-description-skill/SKILL.md) -- authors
  the anchored, mermaid-validated body of the ONE issue PR opened at
  Phase 4 acceptance-close (see `assets/acceptance-observer.md`). The
  PR body is never hand-rolled.

All three are same-repo LOCAL SIBLINGS. This skill DECLARES each
dependency here AND in `apm.yml`, and PROBES for each at its use-site
(Phase 1 triage, Phase 4 PR-open, Phase 5 shepherd) with a tool call --
never an assertion from recall (A9 SUPERVISED EXECUTION).

## Hard boundaries

- MANUAL invocation only. No event triggers, no label triggers, no
  gh-aw. Only existing `status/shepherding` processing metadata may be
  written for bookkeeping, NEVER human decision labels or new labels.
  Load the triage sibling's `assets/label-contract.json` for ownership
  and legacy read compatibility. Labels are NEVER implementation authority.
- Triage is paramount. The autopilot ESCALATES to the maintainer by
  default; auto-implementation is the narrow exception, reached only
  for a clear, bounded, high-confidence accept the maintainer
  approved.
- ONE consolidated triage review for the whole batch, not drop-by-
  drop. One consolidated human checkpoint (Phase 2), renewed on
  resume, scope change, or uncertainty about withdrawal.
- Never auto-merge. Mergeability is the terminal state; the human
  approves the protected merge.
- Escalation NEVER auto-closes or auto-declines an issue. It surfaces
  the issue to the maintainer running the session and leaves it for
  human action (inherits batch-bug-shepherd terminal-handling).

## Architecture invariants

- **Fan-out, not serial.** Triage, solution-pipeline (and its Plan
  lenses + per-wave task children), and shepherd-driver all run as
  parallel child threads via the runtime `task` affordance. A
  single-loop variant is an anti-pattern. Subagent capacity is
  UNLIMITED and is NEVER a deferral reason.
- **Worktree isolation, with one-writer integration.** Each pipeline,
  driver, and per-wave task child has its OWN worktree. Task branches
  start at the wave base; only the pipeline integrates them through
  `git merge --no-ff` at the wave gate. Tasks must touch disjoint files:
  conflicts trigger re-planning, never hand-resolution. Read-only
  triage/verifier/lens children may share REPO_ROOT. Phase 4 loads the
  full provisioning and cleanup procedure from its pipeline prompt.
- **One persisted state table.** A single `plan.md` ground-truth table
  plus a machine-readable `proceed_manifest` is the canonical session
  state (B4 PLAN MEMENTO). Reload it at every phase boundary; never
  keep parallel state in memory. The orchestrator is the SOLE writer
  (one-writer rule); children return JSON, the parent writes rows.
- **Escalate by default.** The confidence gate
  ([assets/confidence-gate-rubric.md](assets/confidence-gate-rubric.md))
  routes anything doubtful to the human. Auto-proceed is the exception.
- **Triage children are advisory-only.** A triage child runs the
  apm-triage-panel rubric and returns structured JSON. It MUST NOT
  post a comment, apply a label, touch the working tree, or use any
  GitHub safe-output channel. A child that emits a comment is a hard
  retry/fail.
- **Fold by default at the PR layer.** Inherited from shepherd-driver:
  every follow-up inside a PR's stated scope is folded; only scope-
  crossing items defer with a one-line boundary note.
- **Deterministic tool bridge.** Every consequential write (label,
  assign, PR open, push, comment, merge probe) and every present-state
  fact (CI status, mergeable, head sha, duplicate existence, PR-in-
  flight) goes through a deterministic CLI (`gh`, `git`, `uv run
  ruff`) wrapped in plan + execute + verify. Never assert these from
  recall.
- **ASCII only.** All artifacts (tables, comments, commits, the
  digest) stay within printable ASCII; status symbols `[+] [!] [x]
  [i] [*] [>]`.

## Dependency probes (run before composing)

Phase 1 (before triage fan-out):

```
test -f ../apm-triage-panel/SKILL.md \
  && echo "apm-triage-panel present" \
  || echo "MISSING apm-triage-panel - stop and ask the operator"
```

Phase 5 (before shepherd-driver fan-out):

```
test -f ../shepherd-driver/assets/shepherd-driver-prompt.md \
  && test -f ../shepherd-driver/assets/completion-schema.json \
  && test -f ../shepherd-driver/scripts/owner_touch_gate.py \
  && echo "shepherd-driver present" \
  || echo "MISSING shepherd-driver - stop and ask the operator"
```

Phase 4 (before a pipeline child opens the issue PR -- the child
re-probes this in its own worktree before `gh pr create`):

```
test -f ../pr-description-skill/SKILL.md \
  && test -f ../pr-description-skill/assets/pr-body-template.md \
  && echo "pr-description-skill present" \
  || echo "MISSING pr-description-skill - stop and ask the operator"
```

On a probe MISS, STOP and ask the operator to restore the sibling; do
NOT re-implement the composed logic inline.

## Phases

Work through the phases in order. Reload the ground-truth table and
`proceed_manifest` at each phase boundary (B8 ATTENTION ANCHOR). Do
not skip the consolidated-review gate (Phase 2).

### Phase 0 - scope and seed

Take the issue list or queue from the maintainer (explicit issue
numbers, a search query, or "the open backlog"). Resolve it to a
concrete set via `gh issue list`. Seed the ground-truth table
([assets/ground-truth-table.md](assets/ground-truth-table.md)) with
one row per issue at `status: pending-triage`. Record the seed source
and HEAD sha in plan.md. No labels are written in this phase.

### Phase 1 - triage fan-out (read-only)

PROBE for apm-triage-panel (above). Then, for EACH issue, spawn ONE
triage child using
[assets/triage-prompt.md](assets/triage-prompt.md), at PLANNER class
(`claude-opus-4.8`) per model-routing.md. Each child runs
the apm-triage-panel rubric in DIRECT mode and returns ONE
`autopilot-triage-decision` JSON matching
[assets/autopilot-triage-schema.json](assets/autopilot-triage-schema.json).
Children are read-only and post nothing.

Each child invokes the triage skill once; no nested panel-of-panels.

On each return: schema-validate, write the row (decision, type,
confidence, red_flags), set `status: triaged`. On a child that posted
a comment or mutated the tree, discard and re-spawn ONCE; on a second
violation, mark the row `blocked` and escalate it in Phase 2.

### Phase 2 - consolidated triage review + the ONE human checkpoint

This is the heart of the skill. Present one consolidated checkpoint
for the batch, with explicit scope confirmation for each issue.

1. For every triaged row, apply
   [assets/confidence-gate-rubric.md](assets/confidence-gate-rubric.md)
   to compute a `gate`: `auto-proceed` | `escalate` | `terminal`.
   Escalate by default; auto-proceed only for a clear, bounded, high-
   confidence accept whose implementation brief is complete.
2. Write the machine-readable `proceed_manifest` into plan.md (one
   row per issue: `issue, gate, maintainer_decision, override_reason,
   implementation_brief_ref, status`). `maintainer_decision` starts
   `pending`.
3. Render ONE consolidated digest from
   [assets/triage-digest-template.md](assets/triage-digest-template.md):
   every issue with its decision, type, confidence, gate, red flags,
   and (for auto-proceed rows) the implementation brief. Present it to
   the maintainer in-session.
4. Capture the maintainer's decision per row: `approved` | `rejected`
   | `overridden` (with `override_reason`). The maintainer may approve
   an escalated row (override to proceed) or reject an auto-proceed
   row. Write the result into the `proceed_manifest`.

This APM-specific consumer must probe the target repository's governance
tool from a trusted default-branch checkout, never a contributor/head
branch or the installed skill directory:

```bash
node scripts/governance/eligibility.cjs --help
node scripts/governance/eligibility.cjs --repo microsoft/apm --issue N --approval-url URL
```

Use the nominated issue-comment scope record. `authority.cjs` is the
single owner of record/roster interpretation and reads GOVERNANCE.md from
the trusted default branch. Do not bundle another parser, infer authority
from labels, or add a dependency to approximate it. Missing trusted tool,
incomplete/API-failed reads, or unverifiable evidence means STOP/escalate.

The JSON always reports `authorizes_implementation: false`. Even an
unedited human evidence record cannot detect deleted withdrawals.
Obtain fresh explicit responsible-human confirmation for the issue's
bounded scope, done-when criteria, exclusions, and review contact; record
that current confirmation reference alongside the approval URL in the
`proceed_manifest`. A named contact is not proof of review availability.
Existing `status/accepted` / `accepted` labels, old `triage-decision`
comments, bot/persona advice, PR reviews, and silence are not approval.
Neither a persona nor the absence of objections can fill `maintainer_decision`.
Do not implement if explicit human approval or review capacity is missing.
Recheck evidence before each mutating wave and renew the human checkpoint
on resume, scope change, or uncertainty about withdrawal.

All later phases select rows ONLY where `gate` resolves to proceed AND
`maintainer_decision in (approved, overridden-to-proceed)`. Rows the
maintainer left escalated/terminal are handled in Phase 7.
Pass the bounded checkpoint receipt to every solution, drive, and
conflict-resolution child, including existing community PRs; no child may
implement or expand scope without it. A stored `approved` value alone
cannot substitute for current confirmation.

### Phase 3 - ownership signaling + PR-in-flight xref

For each proceed row: cross-reference open PRs that already address
the issue (`gh pr list --search`). Record `pr` and `pr_in_flight`.
Do not apply `status/accepted` or any other human decision label.
On the issue (and the PR if one exists), assign `@me` and add
`status/shepherding` only if that processing label already exists; if
absent, record processing state locally and report it, never create a label.
Record every
label THIS run adds in the row's `labels_added` column so Phase 7 (and
Phase 5 teardown) strip ONLY those and never touch pre-existing
labels.

### Phase 4 - solution pipeline (Ideate -> Plan -> Implement waves)

Refresh Phase 2 before provisioning. Pass `TRUSTED_GOVERNANCE_ROOT`,
`APPROVAL_URL`, and `HUMAN_SCOPE_RECEIPT`; the child enforces its current
human-scope gate before each wave and acceptance close.

For each proceed row WITHOUT an in-flight PR, spawn ONE solution-
pipeline child in its OWN git worktree on the issue branch (provision
with `git worktree add` at HEAD; record its slug in the row's
`worktree` column so Phase 7 tears down only worktrees this run
created). Spawn it at IMPLEMENTER class (`claude-sonnet-4.6`); it and
every child it spawns route models per
[assets/model-routing.md](assets/model-routing.md) (B12 MODEL ROUTER).
**Load `assets/solution-pipeline-prompt.md` on entering Phase 4** and
give it to the child. It owns the full four-stage procedure, staffing,
model routing, wave gates, and cleanup; the child is the SOLE WRITER
of the issue branch and returns the opened PR:

1. **Ideate:** devx-ux-expert at PLANNER (`claude-opus-4.8`) derives
   `acceptance_shape` using `assets/ideate-prompt.md`.
2. **Plan:** python-architect and triggered lenses produce the persisted
   task/wave DAG via `assets/plan-panel-prompt.md` and
   `assets/plan-schema.json`. Trivial issues use one task/one wave.
3. **Implement:** one isolated child per task using
   `assets/task-implement-prompt.md`; `assets/wave-gate-rubric.md`
   governs integration and plan-guardian/ideator verification.
   PASS advances; FAIL re-plans from that wave, capped at two re-plans.
4. **Acceptance close:** `assets/acceptance-observer.md` verifies every
   acceptance condition deterministically before ONE PR (`Closes #N`).

Each task child writes the TYPED coverage gate first (bug: failing
regression trap + mutation-break; feature: failing acceptance test;
docs: docs build/link check; refactor/perf: behavior-preserving test +
benchmark) for its task type, never opens a PR, and never spawns
children. The orchestrator then applies the Phase 3 ownership signaling
to the new PR. Rows WITH an in-flight PR skip Phase 4 and go straight
to Phase 5. On `escalate|blocked`, persist its status and reason in the
row and `proceed_manifest`; stop the issue for a renewed human checkpoint,
and do not read PR fields or dispatch Phase 5/6. Surface it in Phase 7.
Only `pr-opened` returns supply PR fields; record `routing_receipts`
in the row's notes for the model-routing audit.

### Phase 5 - shepherd-driver fan-out (drive to merge)

PROBE for shepherd-driver (above). For each PR (own-implemented or in-
flight), spawn ONE shepherd-driver subagent using
`../shepherd-driver/assets/shepherd-driver-prompt.md`. It owns the
convergence loop (Copilot classification, apm-review-panel, fold-vs-
defer, push, CI watch) and returns a `completion_return` matching
`../shepherd-driver/assets/completion-schema.json`. Caps: 4 outer
iterations, 2 Copilot rounds, 3 CI recovery iterations.

On each terminal return: schema-validate, persist the returned JSON in
session state, derive `BASE_SHA` with `git -C <row-worktree> merge-base
<returned-head-sha> origin/main`, and independently run:

```
uv run python <row-worktree>/.agents/skills/shepherd-driver/scripts/owner_touch_gate.py verify \
  --repo-root <row-worktree> --base $BASE_SHA \
  --head <returned-head-sha> --completion <session-return-json>
```

Do not write terminal state until both gates pass. A schema or semantic
failure gets one re-spawn; a second failure marks the row `blocked`
with the verifier diagnostic. This prevents stale evidence or child
self-classification from bypassing canonical owner detection.

After both gates pass, write `head_sha` and the
`mergeable/merge_state_status/ci_status` projection into the row's
`head_sha` and `merge_state` columns (the crash-survivable A11 stop
evidence), and remove ONLY the `status/shepherding` labels listed in
the row's `labels_added` column (assignment stays). Also record the
return's `panel_execution` (`skill-tool`|`inline`), `panel_personas`,
and `routing_receipt` in the row's notes -- the inline panel path is
EXPECTED in subagent context, so `panel_execution: inline` is a normal
healthy value, not a degradation.

### Phase 6 - conflict-resolution

For every PR that returned `ready-to-merge`, probe mergeability (`gh
pr view --json mergeable,mergeStateStatus`). On DIRTY / BEHIND /
CONFLICTING, spawn one conflict-resolution subagent per
`../shepherd-driver/assets/conflict-resolution-prompt.md` (step-by-
step in `../shepherd-driver/references/mergeability-gate.md`).

### Phase 7 - final report

Read the table and `proceed_manifest` one last time. Render
[assets/final-report-template.md](assets/final-report-template.md) to
the maintainer: per-issue decision, gate, maintainer decision, PR
link, terminal status, ready-to-merge PRs, advisory-with-deferred
PRs, blockers (with the responsible child's session ref), and every
ESCALATED / terminal row still awaiting human action. Tear down only
the solution-pipeline/shepherd worktrees recorded in the `worktree`
column (`git worktree remove`); leave branches on origin for open PRs.
Never auto-close an escalated issue.

## Bundled assets

- [assets/triage-prompt.md](assets/triage-prompt.md) -- any-type
  triage child spawn body (runs apm-triage-panel in DIRECT mode).
- [assets/autopilot-triage-schema.json](assets/autopilot-triage-schema.json)
  -- JSON schema for the `autopilot-triage-decision` return.
- [assets/confidence-gate-rubric.md](assets/confidence-gate-rubric.md)
  -- escalate-by-default gate policy (Phase 2).
- [assets/triage-digest-template.md](assets/triage-digest-template.md)
  -- the ONE consolidated review presented to the maintainer.
- [assets/solution-pipeline-prompt.md](assets/solution-pipeline-prompt.md)
  -- A2 PIPELINE: per-issue Ideate -> Plan -> Implement(waves) ->
  Acceptance close (Phase 4 child; sole writer of the issue branch).
- [assets/ideate-prompt.md](assets/ideate-prompt.md) -- devx-ux-expert
  Ideate child: design_brief + testable acceptance_shape (read-only).
- [assets/plan-panel-prompt.md](assets/plan-panel-prompt.md) -- Plan
  stage: lens-selection rubric + python-architect synthesis emitting
  the task DAG.
- [assets/plan-schema.json](assets/plan-schema.json) -- JSON schema for
  the persisted `issue-solution-plan` (tasks, deps, waves, checkpoints).
- [assets/model-routing.md](assets/model-routing.md) -- B12 MODEL ROUTER:
  authoritative role-class -> concrete-model table + per-spawn bindings +
  verifier escalation; the pipeline resolves every Phase 4 spawn's model
  here. Also records the B14b CAVEMAN BRIEF layer (lens advisors +
  wave-gate verifiers ship compressed, fixed-schema briefs), the B14c
  audience-boundary PER-SPAWN DECLARATION TABLE, the B13 cache-aware-
  prefix discipline, and the B15/B16 status for this harness.
- [assets/task-implement-prompt.md](assets/task-implement-prompt.md) --
  ONE task per child in its own worktree; loads the typed coverage gate
  by task type; no PR, no further fan-out.
- [assets/implement-bug.md](assets/implement-bug.md),
  [assets/implement-feature.md](assets/implement-feature.md),
  [assets/implement-docs.md](assets/implement-docs.md),
  [assets/implement-refactor.md](assets/implement-refactor.md) --
  per-type coverage-gate references loaded per task by
  task-implement-prompt.md.
- [assets/wave-gate-rubric.md](assets/wave-gate-rubric.md) -- inter-wave
  checkpoint: integrate + plan-guardian/ideator verify + re-plan policy.
- [assets/acceptance-observer.md](assets/acceptance-observer.md) -- B5
  close: verify acceptance_shape, open the ONE issue PR.
- [assets/ground-truth-table.md](assets/ground-truth-table.md) --
  canonical table template with A11 columns + proceed_manifest.
- [assets/final-report-template.md](assets/final-report-template.md)
  -- end-of-session report to the maintainer.

## Composed siblings (declared dependencies)

- [apm-triage-panel](../apm-triage-panel/SKILL.md) -- triage rubric
  (Phase 1; probed before use).
- [shepherd-driver](../shepherd-driver/SKILL.md) -- per-PR drive-to-
  merge loop + mergeability gate (Phases 5-6; probed before use).
  Its declared completion schema and deterministic owner-touch gate
  are re-run by this parent before terminal state is accepted.
  Transitively composes apm-review-panel.
- [pr-description-skill](../pr-description-skill/SKILL.md) -- authors
  the Phase 4 issue-PR body (probed before the PR-open; never
  hand-rolled). Transitively also used by shepherd-driver for any
  superseding PR.

## Operating contract for the orchestrator thread

- Before each phase: re-read `plan.md` (table + proceed_manifest). Do
  NOT rely on recall.
- The orchestrator is the SOLE writer of the ground-truth table and
  of the GitHub state it explicitly owns (issue labels, assignment,
  tracking issues). PR-side writes -- pushes and PR comments during
  the drive and mergeability phases -- are owned by shepherd-driver,
  never by the orchestrator.
- Every consequential write is plan + execute + verify through a CLI.
- One consolidated review, one human checkpoint, escalate by default.
