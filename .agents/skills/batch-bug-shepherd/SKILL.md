---
name: batch-bug-shepherd
description: >-
  Use this skill to drive a batch of suspected bugs in microsoft/apm
  from raw issue list to mergeable PR queue. Fan out one triage
  subagent per issue (LEGIT / UNCLEAR / FIXED-AT-HEAD), cross-
  reference legit issues against open PRs, then per PR spawn a
  shepherd-driver subagent that runs an iterative convergence loop:
  classify copilot-pull-request-reviewer[bot] inline review, run
  the apm-review-panel, fold (by default) every recommendation that
  raises the bar of the PR's stated scope, push to the contributor
  fork or a superseding PR that preserves authorship via commit
  trailers, watch CI green, and iterate until ship-ready or
  terminal. Assign and label `status/shepherding` on pickup; clear
  on terminal. Maintain a single plan.md ground-truth table as
  canonical session state. Activate when the maintainer asks to
  triage a list of issues, sweep the bug queue, shepherd
  bug-flagged issues, run a weekly community-bug sweep, or drive
  in-flight community PRs to merge -- even if "shepherd" or
  "batch" is not named.
---

# batch-bug-shepherd - Outer-loop bug-queue orchestrator

Design record: [design.md](design.md). This SKILL.md is the natural-
language module derived from that design; refactors update both
files in lockstep.

This skill is an A10 ORCHESTRATOR-SAGA over four fan-out waves
(triage, alignment, fix, shepherd-driver) with a persisted ground-
truth table between phases. It COMPOSES the
[apm-review-panel](../apm-review-panel/SKILL.md) skill -- it does
NOT re-implement panel review. Per-PR convergence (Copilot
classification, panel run, fold-vs-defer, push, CI watch) is
delegated to the shepherd-driver subagent.

The skill is ADVISORY at the panel layer and EXECUTIVE at the
orchestrator layer: it WILL push commits, open PRs, post comments,
close superseded PRs, assign issues/PRs, and apply or remove the
`status/shepherding` label. Every consequential write goes through
a deterministic CLI (`gh`, `git`, `uv run ruff`) wrapped in plan +
execute + verify (A9 SUPERVISED EXECUTION).

## Architecture invariants

- **Fan-out, not serial.** Triage, alignment, fix, and shepherd-
  driver all run as parallel child threads via the runtime's `task`
  affordance. A single-loop variant of this skill is an anti-
  pattern. Subagent capacity is UNLIMITED and is NEVER a deferral
  reason.
- **Default is fold. Defer is the exception.** This is the load-
  bearing discipline. When the panel CEO or
  `copilot-pull-request-reviewer[bot]` surfaces a follow-up, the
  shepherd-driver applies `assets/fold-vs-defer-rubric.md` to
  decide. The rubric's axis is SCOPE-CREEP RISK relative to the
  PR's stated scope, NOT severity and NOT separability. Items that
  raise the quality bar of the stated scope (missing tests for new
  behavior, CHANGELOG entry, doc drift caused by the change,
  warning ergonomics on the new surface, security hardening on the
  new code path) are FOLDED. Items that introduce a wholly
  different theme (a cross-cutting refactor sparked by a one-file
  fix) are DEFERRED, and the deferral MUST carry a one-line
  `scope_boundary_crossed` note.
- **Copilot review is a first-class signal.** Phase X.0 of every
  shepherd-driver iteration fetches
  `copilot-pull-request-reviewer[bot]` review and inline comments,
  classifies each item LEGIT / NOT-LEGIT per
  `assets/copilot-classification-prompt.md`, and folds LEGIT items
  into the same commit family as panel follow-ups. Max 2 Copilot
  rounds per shepherd-driver run.
- **CI must be observed green.** After every push the shepherd-
  driver runs the CI watch + recovery loop per
  `assets/ci-recovery-checklist.md`. Transient infra hiccups get
  one `gh run rerun --failed`; persistent failures get a fix. Hard
  cap 3 CI fix iterations per shepherd-driver run; on cap hit, the
  PR is marked `blocked` with the failing job + log excerpt in the
  return.
- **Ownership signaling on pickup.** The orchestrator assigns
  itself (`gh issue/pr edit <n> --add-assignee @me`) and applies
  `status/shepherding` (`--add-label status/shepherding`) the
  moment BBS touches an issue or PR. The label is created on
  demand (`gh label create status/shepherding --color B8860B
  --description "Actively being driven by an APM shepherd run"
  --force --repo microsoft/apm`). On shepherd-driver terminal
  return the label is removed; assignment stays (or transfers to
  the original PR author for community PRs).
- **Iteration cap is the safety valve.** Hard cap 4 outer
  iterations per shepherd-driver per PR. On cap hit, post the
  final advisory with explicit "remaining items + deferral
  rationale" and stop. Row is marked `advisory-with-deferred` in
  the table.
- **Verify before fix.** No fix subagent is dispatched until the
  issue is reproduced on HEAD (verdict `LEGIT`). `UNCLEAR` issues
  are surfaced for human triage; `FIXED-AT-HEAD` issues are
  recommended for close.
- **PR-in-flight detection is mandatory.** Before dispatching ANY
  fix, the orchestrator runs `gh pr list --search "<issue-ref>"`
  (and scans linked PRs on the issue) for every legit issue.
  Skipping this step risks duplicating community work, which is
  the worst failure mode this skill defends against.
- **Mutation-break gate.** A regression-trap test is REAL only when
  deleting the production guard makes it FAIL. Tests that pass
  with the guard deleted are logic-replay, not regression traps.
  Enforced in both the fix subagent and the shepherd-driver
  whenever a new regression test is added.
- **Superseding-PR fallback.** When push to the contributor fork
  fails (no `maintainerCanModify`, branch protection, or fork
  deleted), the shepherd-driver opens a new PR under
  `microsoft/apm` that PRESERVES AUTHOR AUTHORSHIP via `git
  cherry-pick` and `Co-authored-by:` trailers. Original PR closed
  with a courteous handoff comment.
- **Single-writer interlock per artifact.** The apm-review-panel
  run posts exactly ONE comment per panel pass (its own contract).
  The shepherd-driver does not post additional inline comments to
  the PR; its on-PR footprint is the panel comment (re-rendered as
  iterations converge) augmented by the appended "Folded /
  Copilot signals / Deferred" sections from
  `assets/final-report-template.md`.
- **No verdict labels.** BBS never applies `panel-approved` /
  `panel-rejected` / any merge gate. The panel removed those; BBS
  honors the removal. The maintainer ships.
- **ASCII only.** All artifacts (table, comments, commit messages,
  templates, prompts) use printable ASCII. No emojis, no em dashes,
  no unicode box-drawing. Windows cp1252 terminals will
  UnicodeEncodeError on anything else.
- **Lint contract is the push gate.** Before any `git push`, both
  `uv run --extra dev ruff check src/ tests/` and `uv run --extra
  dev ruff format --check src/ tests/` MUST be silent. See
  `.github/instructions/linting.instructions.md`.
- **Ground-truth table is the single source of truth.** One
  markdown table in the session's plan.md, rewritten on every
  subagent return. Schema in `assets/ground-truth-table.md`.
  Re-read it at the start of every wave (B4 PLAN MEMENTO + B8
  ATTENTION ANCHOR).
- **Cross-session message reports only on terminal.** The
  shepherd-driver messages the orchestrator only with one of the
  four terminal statuses: `ready-to-merge`,
  `advisory-with-deferred`, `superseded`, `blocked`. Mid-loop
  state stays in the subagent's context.

## Composition with apm-review-panel

`apm-review-panel` is the per-pass review primitive. The shepherd-
driver invokes it ONCE per outer iteration (so up to 4 times per
PR). The orchestrator does not invoke it directly.

Per the panel skill's idempotency contract, successive panel runs
on the same PR rewrite the same recommendation comment surface --
the shepherd-driver does NOT clean up earlier in-loop panel
comments, and does not block on duplicate emission.

The shepherd-driver consumes:

1. The CEO `ship_recommendation.stance` (`ship_now` /
   `ship_with_followups` / `needs_discussion` / `needs_rework`).
2. The CEO `recommended_followups` array.

These feed the fold-vs-defer decision per
`assets/fold-vs-defer-rubric.md`. The orchestrator never reaches
into apm-review-panel internals.

## Phases

Work through the phases in order. Reload the ground-truth table at
each phase boundary. Do not skip the cross-reference phase.

### Phase 0 - scope resolution

Input is either (a) an explicit issue list (e.g. `#123 #456 #789`)
or (b) the `sweep-all` flag, which expands to:

- `gh issue list --label bug --state open --json number,title,labels,body`
- plus `gh issue list --state open --search "is:open no:label"`
  filtered by suspicion keywords (`error`, `crash`, `broken`,
  `regression`, `unexpected`, `traceback`, `does not work`,
  `cannot`, `fails`).

Initialize the ground-truth table (`assets/ground-truth-table.md`)
with one row per candidate. Print a brief plan to the user:
candidate count, expected wave shape, and the disciplines that
will be enforced (fold-by-default, mutation-break, ASCII, lint,
CI verification). Ask for confirmation only if `sweep-all`
produced more than 20 candidates -- otherwise proceed.

### Phase 1 - triage fan-out (WAVE 1)

Spawn one child thread per candidate using `assets/triage-prompt.md`.
Each subagent reproduces the bug on HEAD and returns a verdict JSON
matching `assets/verdict-schema.json` (`triage` shape).

Schema-validate every return. On malformed, re-spawn once. On
second malformed, mark `UNCLEAR -- subagent malformed` and continue.
Update the table after every return.

### Phase 1.5 - strategic-alignment fan-out

For every `LEGIT` row, spawn a `ceo-align` subagent using
`assets/strategic-alignment-prompt.md`. Each subagent activates the
apm-ceo persona and returns one `strategic_alignment_return` JSON.
Gate fails OPEN to `aligned` on any infrastructure failure
(missing persona, missing PRINCIPLES.md, second malformed return).
NEVER demote a legit bug without a citable principle.

### Phase 2 - PR-in-flight cross-reference + ownership signaling

For every `LEGIT && aligned` row:

1. `gh pr list --search "<issue-ref-or-keywords>" --state open
   --json number,title,headRefName,author,maintainerCanModify`.
   Also inspect linked PRs on the issue.
2. Two outcomes per row:
   - `pr_in_flight = false` -> route to FIX (Phase 3).
   - `pr_in_flight = true` -> capture PR number, author, fork URL,
     `maintainerCanModify` flag. Route to SHEPHERD-DRIVER
     (Phase 4).
3. **Ownership signaling.** Ensure the label exists:
   ```
   gh label create status/shepherding --color B8860B \
      --description "Actively being driven by an APM shepherd run" \
      --force --repo microsoft/apm
   ```
   Then for every picked-up issue AND any in-flight PR:
   ```
   gh issue edit <issue> --add-assignee @me \
      --add-label status/shepherding --repo microsoft/apm
   gh pr edit <pr> --add-assignee @me \
      --add-label status/shepherding --repo microsoft/apm
   ```
   If label create or label add fails (e.g. perms), continue
   without the label and record a one-line warning in the final
   report -- ownership-signaling is helpful but not load-bearing
   for correctness.

Update the table. This phase MUST complete before any phase-3 or
phase-4 spawn.

### Phase 3 - fix fan-out (WAVE 2, greenfield only)

For each `LEGIT && aligned && !pr_in_flight` row, spawn a child
thread with `assets/fix-prompt.md`. The fix subagent writes failing
tests FIRST, implements the minimum fix, runs the mutation-break
gate, runs the lint contract, opens a PR, and returns the PR
number. When the fix subagent returns a PR number, the orchestrator
immediately applies the same `--add-assignee @me +
status/shepherding` signaling per Phase 2.

### Phase 4 - shepherd-driver fan-out (WAVE 3)

For each PR (both Phase 2 community PRs and Phase 3 own-fix PRs),
spawn ONE shepherd-driver subagent using
`assets/shepherd-driver-prompt.md`. That subagent owns the
convergence loop end-to-end:

1. Phase X.0 -- fetch + classify
   `copilot-pull-request-reviewer[bot]` per
   `assets/copilot-classification-prompt.md`.
2. Phase X.1 -- invoke `apm-review-panel` skill against the PR.
3. Phase X.2 -- merge follow-ups (LEGIT Copilot + panel
   `recommended_followups`), apply fold-vs-defer rubric per
   `assets/fold-vs-defer-rubric.md`.
4. Phase X.3 -- edit code, fold every FOLD item. Mutation-break
   gate on any new regression-trap test.
5. Phase X.4 -- lint contract silent.
6. Phase X.5 -- push (author fork; fall back to superseding PR).
7. Phase X.6 -- CI watch + recovery per
   `assets/ci-recovery-checklist.md` (cap 3).
8. Phase X.7 -- decide terminal vs next iteration.

Caps: 4 outer iterations; 2 Copilot rounds; 3 CI recovery
iterations.

Terminal returns: `ready-to-merge` (clean convergence),
`advisory-with-deferred` (iteration cap hit with foldable items
remaining; rare), `superseded` (push fell back to superseding PR),
or `blocked` (CI cap hit, panel unavailable, or unresolvable
scope conflict).

On every terminal return: orchestrator schema-validates, updates
the table, and removes the `status/shepherding` label from the PR
and the linked issue (assignment stays).

### Phase 5 - conflict-resolution

For every PR that returned `ready-to-merge`, probe mergeability
(`gh pr view --json mergeable,mergeStateStatus`). On DIRTY /
BEHIND / CONFLICTING, spawn one conflict-resolution subagent per
`assets/conflict-resolution-prompt.md`. That subagent rebases,
resolves conflicts faithfully, lint-gates, re-probes, and posts
ONE resolution-confirmation comment.

### Phase 6 - final report

Read the table one last time. Render
`assets/final-report-template.md` (FINAL REPORT block) to the
user: per-issue verdict, PR link, ready-to-merge status, advisory-
with-deferred PRs, unresolved blockers (with the responsible
subagent's session reference), and any rows still `UNCLEAR` for
human triage. The "Disciplines honored" section reports the new
metrics: folded vs deferred totals, Copilot rounds, CI iterations,
mutation-break tests added, ownership labels cleared.

## Bundled assets

- `assets/verdict-schema.json` -- JSON schema for triage,
  strategic-alignment, shepherd, and completion (shepherd-driver
  return) shapes. Schema-validate every subagent return.
- `assets/ground-truth-table.md` -- canonical table template.
  Includes the new `shepherd-driver-iter-{1..4}` and
  `advisory-with-deferred` status values.
- `assets/triage-prompt.md` -- spawn body for WAVE 1.
- `assets/strategic-alignment-prompt.md` -- spawn body for WAVE
  1.5.
- `assets/fix-prompt.md` -- spawn body for WAVE 2 (greenfield
  fixes).
- `assets/shepherd-driver-prompt.md` -- spawn body for WAVE 3.
  Replaces the previous shepherd / completion split.
- `assets/fold-vs-defer-rubric.md` -- decision rubric consumed by
  shepherd-driver Phase X.2.
- `assets/copilot-classification-prompt.md` -- LEGIT/NOT-LEGIT
  classification template consumed by shepherd-driver Phase X.0.
- `assets/ci-recovery-checklist.md` -- post-push CI watch +
  recovery loop consumed by shepherd-driver Phase X.6 and by
  fix-prompt on initial-push red CI.
- `assets/conflict-resolution-prompt.md` -- spawn body for Phase 5.
- `assets/final-report-template.md` -- user-facing final report
  AND PR advisory comment shape (consumed by the shepherd-driver
  in the terminal step).
- `assets/progress-diagram.md` -- operator visibility mermaid for
  phase progression.

Removed in this refactor (justification in `design.md`):

- `assets/shepherd-prompt.md` -- replaced by
  `shepherd-driver-prompt.md`. The old prompt stopped after one
  panel pass and posted advisory; this baked in the gap-1
  anti-pattern (recommendations as backlog).
- `assets/completion-prompt.md` -- absorbed into
  `shepherd-driver-prompt.md`. Splitting the fold + push + CI
  watch + Copilot loop across two prompts re-introduces the
  broken handoff seam.

## Operating contract for the orchestrator thread

- Before each phase: re-read `plan.md` ground-truth table. Do NOT
  rely on recall from earlier phases.
- After each subagent return: schema-validate, update the table,
  write it back to `plan.md`.
- Apply ownership signaling (`--add-assignee @me +
  status/shepherding`) the moment any issue or PR enters BBS scope.
  Remove the label on shepherd-driver terminal.
- Never post to a PR directly. Delegate every PR-side write to the
  shepherd-driver subagent.
- Never skip the cross-reference phase. The "duplicates community
  work" failure mode is more expensive than every other failure
  mode this skill defends against, combined.
- Never override the fold-by-default discipline from the
  orchestrator side. The rubric lives in the subagent prompt; the
  orchestrator does not freelance deferrals.
- Honor the lint, ASCII, and ownership-signaling rules
  transitively: every spawn prompt reminds its subagent of them.

## Out of scope

- Authoring panel personas (lives in `apm-review-panel`).
- Computing coverage percentages (lives in test-coverage-expert
  persona, invoked via apm-review-panel).
- Single-PR review without a batch (use `apm-review-panel`
  directly).
- Auto-merge or auto-label with verdict labels. The orchestrator
  does not flip merge state and does not apply
  `panel-approved` / `panel-rejected`; the maintainer ships.
