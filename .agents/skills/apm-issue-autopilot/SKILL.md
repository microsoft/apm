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

This SKILL.md is the natural-language module derived from a genesis
design packet; refactors re-run the genesis skill from that packet.

This skill is an A11 RECONCILIATION LOOP, MANUALLY INVOKED, over a
queue of issues each driven to a terminal state under non-determinism.
It generalizes [batch-bug-shepherd](../batch-bug-shepherd/SKILL.md)
from bugs-only to ANY issue type, and promotes the full
[apm-triage-panel](../apm-triage-panel/SKILL.md) rubric to the central
front gate. It does NOT re-implement triage, panel review, or the
per-PR convergence loop -- it COMPOSES existing skills.

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
  gh-aw. Labels (`status/accepted`, `status/shepherding`) are WRITTEN
  for bookkeeping but are NEVER the trigger.
- Triage is paramount. The autopilot ESCALATES to the maintainer by
  default; auto-implementation is the narrow exception, reached only
  for a clear, bounded, high-confidence accept the maintainer
  approved.
- ONE consolidated triage review for the whole batch, not drop-by-
  drop. Exactly one human checkpoint (Phase 2).
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
- **Worktree isolation, with one-writer integration.** Every solution-
  pipeline and shepherd-driver child runs in its OWN git worktree (one
  per issue/PR). Within Phase 4, a pipeline child further spawns ONE
  task child per task PER WAVE, each in its OWN worktree branched off
  the issue branch at the wave base; the pipeline child is the SOLE
  WRITER of the issue branch and integrates task branches via `git
  merge --no-ff` at the wave gate. Tasks in a wave are mutually
  independent and touch disjoint files by construction (planner-
  enforced), so integration is conflict-free -- a real conflict is a
  planning error and triggers a re-plan, never a hand-resolve. Do NOT
  fan out against one shared working tree. Triage and verifier/lens
  children are read-only and may share a read-only REPO_ROOT.
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
(`claude-opus-4.8`) per model-routing.md (Phase 1 triage binding -- the
paramount front gate is front-loaded heavy: a wrong accept burns a whole
downstream pipeline). Each child runs
the apm-triage-panel rubric in DIRECT mode and returns ONE
`autopilot-triage-decision` JSON matching
[assets/autopilot-triage-schema.json](assets/autopilot-triage-schema.json).
Children are read-only and post nothing.

This is "A11 per-item child thread running an existing triage module",
not a nested panel-of-panels: each child invokes apm-triage-panel
(which itself spawns no sub-agents) once and returns its decision.

On each return: schema-validate, write the row (decision, type,
confidence, red_flags), set `status: triaged`. On a child that posted
a comment or mutated the tree, discard and re-spawn ONCE; on a second
violation, mark the row `blocked` and escalate it in Phase 2.

### Phase 2 - consolidated triage review + the ONE human checkpoint

This is the heart of the skill and the single human gate. Do it ONCE
for the whole batch, not per issue.

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

All later phases select rows ONLY where `gate` resolves to proceed AND
`maintainer_decision in (approved, overridden-to-proceed)`. Rows the
maintainer left escalated/terminal are handled in Phase 7.

### Phase 3 - ownership signaling + PR-in-flight xref

For each proceed row: cross-reference open PRs that already address
the issue (`gh pr list --search`). Record `pr` and `pr_in_flight`.
Apply `status/accepted` to the issue and, on the issue (and the PR if
one exists), assign `@me` and add `status/shepherding`. Record every
label THIS run adds in the row's `labels_added` column so Phase 7 (and
Phase 5 teardown) strip ONLY those and never touch pre-existing
labels.

### Phase 4 - solution pipeline (Ideate -> Plan -> Implement waves)

For each proceed row WITHOUT an in-flight PR, spawn ONE solution-
pipeline child in its OWN git worktree on the issue branch (provision
with `git worktree add` at HEAD; record its slug in the row's
`worktree` column so Phase 7 tears down only worktrees this run
created). Spawn it at IMPLEMENTER class (`claude-sonnet-4.6`); it and
every child it spawns route models per
[assets/model-routing.md](assets/model-routing.md) (B12 MODEL ROUTER).
The child runs
[assets/solution-pipeline-prompt.md](assets/solution-pipeline-prompt.md),
a four-stage per-issue pipeline (A2 PIPELINE), and returns the opened
PR. It is the SOLE WRITER of the issue branch:

1. **Ideate** ([assets/ideate-prompt.md](assets/ideate-prompt.md),
   devx-ux-expert) -- frame the brief and derive a testable
   `acceptance_shape` (the B5 contract). Spawn at PLANNER class
   (`claude-opus-4.8`) per model-routing.md -- front-loaded heavy
   because this contract is the verification spine for every wave.
2. **Plan** ([assets/plan-panel-prompt.md](assets/plan-panel-prompt.md),
   python-architect lead + conditional performance / test-coverage /
   supply-chain / auth lenses) -- emit a persisted task DAG matching
   [assets/plan-schema.json](assets/plan-schema.json): per-task
   staffing, interdependencies, ordering, and WAVES with checkpoints
   (B4 PLAN MEMENTO). Trivial issue -> ONE task in ONE wave; skip the
   full gate ceremony (scale-down).
3. **Implement** (A5 WAVE EXECUTION) -- per wave, spawn ONE task child
   ([assets/task-implement-prompt.md](assets/task-implement-prompt.md))
   per task, each in its OWN worktree off the issue branch at the wave
   base; the pipeline integrates the task branches into the issue
   branch, then runs the inter-wave checkpoint
   ([assets/wave-gate-rubric.md](assets/wave-gate-rubric.md), plan-
   guardian + ideator verifiers). PASS advances; FAIL re-plans from the
   failed wave (cap 2 re-plans).
4. **Acceptance close**
   ([assets/acceptance-observer.md](assets/acceptance-observer.md), B5)
   -- verify every `acceptance_shape` condition deterministically, then
   open ONE PR (`Closes #N`) and return its number.

Each task child writes the TYPED coverage gate first (bug: failing
regression trap + mutation-break; feature: failing acceptance test;
docs: docs build/link check; refactor/perf: behavior-preserving test +
benchmark) for its task type, never opens a PR, and never spawns
children. The orchestrator then applies the Phase 3 ownership signaling
to the new PR. Rows WITH an in-flight PR skip Phase 4 and go straight
to Phase 5. On a `status: escalate|blocked` return, write the reason to
the row and surface it in Phase 7 (no PR opened). On a `pr-opened`
return, also record the child's `routing_receipts` array in the row's
notes (B12 cost audit) so the Ideate=opus / architect=opus front-load
is auditable from plan.md alone, never from a child transcript.

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
