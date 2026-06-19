---
name: batch-bug-shepherd
description: >-
  Use this skill to drive a batch of suspected bugs in microsoft/apm
  from raw issue list to mergeable PR queue. Fan out one triage
  subagent per issue (LEGIT / UNCLEAR / FIXED-AT-HEAD), gate every
  legit bug against PRINCIPLES.md via an apm-ceo strategic-alignment
  pass, cross-reference legit issues against open PRs, then open a fix
  PR (TDD + mutation-break gate) for greenfield bugs. Drive every PR
  -- community in-flight and own fix alike -- to mergeable by
  composing the shepherd-driver skill: one driver per PR runs the
  review panel, folds non-blocking recommendations, pushes (preserving
  author), and watches CI to green. Re-probe mergeability and resolve
  conflicts via shepherd-driver. Maintain a plan.md ground-truth
  table as canonical state. Activate when the maintainer asks to
  triage issues, sweep the bug queue, shepherd bug-flagged issues,
  run a weekly community sweep, or drive in-flight community PRs to
  merge -- even if "shepherd" or "batch" is not named.
---

# batch-bug-shepherd - Outer-loop bug-queue orchestrator

This skill is an A10 ORCHESTRATOR-SAGA over fan-out waves (triage,
strategic-alignment, PR-cross-reference, fix, drive-to-merge,
conflict-resolution) with a persisted ground-truth table between
phases. It COMPOSES the
[shepherd-driver](../shepherd-driver/SKILL.md) skill as the per-PR
drive-to-merge engine -- it does NOT re-implement the review +
fold + push + CI loop. shepherd-driver transitively COMPOSES
[apm-review-panel](../apm-review-panel/SKILL.md); this skill inherits
that edge and never reaches into panel internals directly. It also
COMPOSES the `apm-ceo` persona (host-repo agent at
`.apm/agents/apm-ceo.agent.md`) for the strategic-alignment gate,
which checks every LEGIT bug against `PRINCIPLES.md` before allowing
fix / drive work to proceed. Per-PR shepherding is delegated to
shepherd-driver; per-issue verification, strategic alignment,
PR-in-flight branching, greenfield fix dispatch, post-wave
mergeability re-probe, and the cross-session table are owned here.

The skill is ADVISORY at the panel layer and EXECUTIVE at the
orchestrator layer: it WILL push commits, open PRs, post comments,
close superseded PRs. Every consequential write goes through a
deterministic CLI (`gh`, `git`, `uv run ruff`) wrapped in plan +
execute + verify (A9 SUPERVISED EXECUTION).

## Architecture invariants

These 17 rules bind every wave. The one-line essence is below; the
FULL binding text (rationale, edge cases, inherited-from-driver
detail) lives in `references/invariants.md`. **Load
`references/invariants.md` before planning Phase 0** -- the summaries
here are dispatch anchors, not the complete contract.

- **Fan-out, not serial.** Triage / alignment / fix / drive run as
  parallel child threads; single-loop is an anti-pattern.
- **Verify before fix.** No fix dispatched until the bug reproduces
  on HEAD (`LEGIT`); `UNCLEAR` -> human, `FIXED-AT-HEAD` -> close.
- **PR-in-flight detection is mandatory.** `gh pr list` every legit
  issue before any fix; duplicating community work is the worst
  failure mode this skill defends against.
- **Drive, do not split shepherd from complete.** ONE shepherd-driver
  subagent owns the whole per-PR loop; no separate panel + completion
  waves.
- **Mutation-break gate.** A regression trap is real only if deleting
  the production guard makes the test FAIL.
- **Superseding-PR fallback (inherited).** On contributor-fork push
  failure the driver opens an authorship-preserving PR under
  `microsoft/apm` and returns `superseded`.
- **Single-writer interlock.** One idempotent panel comment + one
  driver advisory per PR; the orchestrator never posts to a PR.
- **ASCII only.** Printable ASCII in every artifact (cp1252 safety).
- **Lint contract is the push gate (inherited).** `ruff check` +
  `ruff format --check` silent before any `git push`.
- **Ground-truth table is the single source of truth.** One plan.md
  table, rewritten on every return, re-read at each wave start
  (B4 PLAN MEMENTO + B8 ATTENTION ANCHOR).
- **Cross-session message reports only on green.** Failures stay in
  the subagent session until resolved or escalated to a human.
- **Operator visibility is a contract.** Progress mermaid + live
  table at every boundary; dispatch table before every fan-out
  (`assets/progress-diagram.md`).
- **Mergeability is post-wave truth.** Re-probe `mergeStateStatus`
  before claiming ready; Phase 5 resolves conflicts with
  `--force-with-lease` (bare `--force` prohibited).
- **Two-comment-per-PR cap.** Driver advisory + resolution
  confirmation only; the in-loop panel comment is idempotent and does
  not add to the count. No third comment, ever.
- **Bias toward folding (inherited).** The driver folds in-scope
  follow-ups into the PR; only genuinely separable work becomes a
  tracking issue.
- **Strategic-alignment gate before drive.** Phase 1.5 runs one
  `apm-ceo` subagent per LEGIT row; demoted rows skip Phase 2-5; the
  gate fails open to `aligned`, aborts only if the persona /
  `PRINCIPLES.md` is missing.
- **Worktree isolation.** Every fix and drive child runs in its OWN
  git worktree (one per issue/PR); never fan out mutating children
  against a shared `REPO_ROOT` (they would race on `.git/index` and
  the checked-out branch). Triage is read-only and may share one.

## Composition with shepherd-driver

`shepherd-driver` is the per-PR drive-to-merge engine. This skill
spawns ONE shepherd-driver subagent per PR (both in-flight community
PRs and own greenfield fix PRs) using the spawn body
`../shepherd-driver/assets/shepherd-driver-prompt.md`. The driver owns
the whole convergence loop -- Copilot classification, apm-review-panel,
fold-vs-defer, push, CI watch, with its own caps -- and
returns a `completion_return` matching
`../shepherd-driver/assets/completion-schema.json`
(`ready-to-merge` | `advisory-with-deferred` | `superseded` |
`blocked`).

The cross-PR conflict-resolution / mergeability phase is ALSO
shepherd-driver's: Phase 5 delegates to
`../shepherd-driver/assets/conflict-resolution-prompt.md` with the
step-by-step gate in
`../shepherd-driver/references/mergeability-gate.md`.

shepherd-driver is a same-repo LOCAL SIBLING declared in `apm.yml`
(`dependencies.apm: [../shepherd-driver]`). PROBE for it before the
drive wave -- a tool call, not an assertion from recall (A9 SUPERVISED
EXECUTION):

```
test -f ../shepherd-driver/assets/shepherd-driver-prompt.md \
  && test -f ../shepherd-driver/assets/completion-schema.json \
  && echo "shepherd-driver present" \
  || echo "MISSING shepherd-driver - stop and ask the operator"
```

On a probe MISS, STOP and ask the operator to restore the sibling; do
NOT re-implement the loop inline (avoids HAND-ROLLED HALLUCINATION and
PHANTOM DEPENDENCY). The driver transitively composes
`apm-review-panel` and probes for it at its own preflight, returning
`status: blocked` on a miss. The orchestrator NEVER reaches into
shepherd-driver or apm-review-panel internals.

## Phases

Work through the phases in order. Reload the ground-truth table at
each phase boundary. Do not skip the cross-reference phase.

At every phase boundary (and once at the run start, once at the
end), render the progress mermaid diagram + the live ground-truth
table to chat per `assets/progress-diagram.md`. Before every
fan-out wave, also render the dispatch table mapping subagent_id to
target. These are not optional -- they are the operator's only
real-time window into a multi-wave parallel saga.

### Phase 0 - scope resolution

Input is either (a) an explicit issue list (e.g. `#123 #456 #789`) or
(b) the `sweep-all` flag, which expands to:
- `gh issue list --label bug --state open --json number,title,labels,body`
- plus `gh issue list --state open --search "is:open no:label"` filtered
  by suspicion keywords (`error`, `crash`, `broken`, `regression`,
  `unexpected`, `traceback`, `does not work`, `cannot`, `fails`).

Initialize the ground-truth table (`assets/ground-truth-table.md`)
with one row per candidate. Print a brief plan to the user:
candidate count, expected wave shape, and the disciplines that will
be enforced (mutation-break, ASCII, lint). Ask for confirmation only
if `sweep-all` produced more than 20 candidates -- otherwise proceed.

Then render the progress mermaid diagram for the first time per
`assets/progress-diagram.md` -- every phase `pending`, with the
candidate count `N` substituted into the P0 and P1 labels. Print
the live (empty) ground-truth table below it. This is the
operator's anchor frame for the run.

### Phase 1 - triage fan-out (WAVE 1)

Re-render the progress diagram with `P1` styled `active`. Print the
dispatch table mapping each `triage-<issue>` subagent_id to its
target issue BEFORE issuing the parallel spawns.

Spawn one child thread per candidate using `assets/triage-prompt.md`.
Each subagent:
- Reproduces the bug on HEAD via the smallest possible repro.
- Returns a verdict JSON matching `assets/verdict-schema.json`
  (`triage` verdict shape).

Schema-validate every return (S4). On malformed, re-spawn that
subagent ONCE with a clarifying note. On second malformed, mark the
row `UNCLEAR -- subagent malformed` and continue.

Update the table. Move on only when every row has a triage verdict.

### Phase 1.5 - strategic-alignment gate (WAVE 1.5)

Re-render with `P15` `active` (substitute `L` LEGIT count). If
`L = 0`, render P1.5 as `skipped` and pass through.

**Load `references/strategic-alignment-gate.md` when entering this
phase** -- it holds the binding procedure (external-dep probes,
fail-open semantics, deferred-PR strategic-rejection subagent).

Probe `.apm/agents/apm-ceo.agent.md` and `PRINCIPLES.md`. Either
missing -> ABORT. Print the dispatch table for the
`ceo-align-<issue>` subagents, then spawn `L` parallel threads with
`assets/strategic-alignment-prompt.md`. Returns are
`strategic_alignment_return` JSON (verdict in `aligned` |
`aligned-with-reservations` | `out-of-scope` | `wrong-direction`).
Schema-validate per retry-once; on second malformed, route as
`aligned` with `gate_note` (fail-open).

Update `strategic_verdict` + `strategic_rationale` columns.
Demoted rows flip to status `triaged-deferred` and are SKIPPED by
Phase 2/3/4/5. `aligned-with-reservations` rows stay in saga;
downstream phases MUST surface the reservations.

### Phase 2 - PR-in-flight cross-reference

Re-render the progress diagram with `P1` `done` and `P2` `active`.
Substitute `L` (LEGIT row count) into the P2 label.

Skip every row with status `triaged-deferred` (Phase 1.5 demoted).
Run a LIGHTWEIGHT `gh pr list` probe against demoted rows only to
feed the deferred-PR strategic-rejection comment procedure in
`references/strategic-alignment-gate.md`; this read-only probe
does not route demoted rows back into Phase 2.

For every `LEGIT` row (status `triaged`), run `gh pr list --search
"<issue-ref-or-keywords>" --state open --json
number,title,headRefName,headRepository,headRepositoryOwner,author,maintainerCanModify`.
Also inspect each linked PR on the issue itself. Two outcomes per row:

- `pr_in_flight = false` -> route to FIX in Phase 3 (greenfield).
- `pr_in_flight = true` -> capture and store, for the Phase 4 driver
  spawn, every input shepherd-driver requires: `PR_NUMBER` (number),
  `AUTHOR` (`author.login`), `HEAD_REPO`
  (`headRepositoryOwner.login` + "/" + `headRepository.name`),
  `HEAD_BRANCH` (`headRefName`), `MAINTAINER_CAN_MODIFY`
  (`maintainerCanModify`), and `ORIGIN = community`. Route to DRIVE in
  Phase 4.

Store these driver-input fields in the row (table columns
`head_repo`, `head_branch`, `maintainer_can_modify`). They are the
crash-survivable evidence the Phase 4 spawn reads -- never re-derived
from recall. Update the table. This phase MUST complete before any
Phase 3 or Phase 4 spawn.

### Phase 3 - greenfield fix fan-out (WAVE 2)

Re-render the progress diagram with `P0..P2` `done` and `P3` `active`.
Substitute `m` (greenfield count) into the P3 label. If `m = 0`,
render P3 as `skipped` (dashed border) and pass straight to Phase 4.

This wave is FIX-ONLY. In-flight community PRs do NOT pass through
here -- they go directly to the Phase 4 drive wave. Filter out any row
with status `triaged-deferred` (strategically demoted by Phase 1.5).

Print the `fix-<issue>` dispatch table (subagent_id -> issue number)
BEFORE spawning. For each `LEGIT && !pr_in_flight` row, provision one
git worktree (`git worktree add <path> origin/main`), record its slug
in the row's `worktree` column, and spawn a child thread with
`assets/fix-prompt.md` passing that worktree path as `REPO_ROOT` (per
the Worktree-isolation invariant). The fix subagent:
- Writes failing tests FIRST (TDD).
- Implements the minimum fix.
- Runs the mutation-break gate (delete the new guard, confirm tests
  FAIL).
- Runs the lint contract.
- Opens a PR under `microsoft/apm` referencing the issue.
- Returns `{kind, issue, pr, branch}`.

On each return, store the Phase 4 driver inputs for the new PR:
`PR_NUMBER` (pr), `AUTHOR` = the maintainer's own gh handle (these are
own PRs), `HEAD_REPO = microsoft/apm` (same-repo head),
`HEAD_BRANCH` (branch), `MAINTAINER_CAN_MODIFY = true`, and
`ORIGIN = own-fix`. Write `head_repo`, `head_branch`,
`maintainer_can_modify` into the row. Hold until every spawn returns.

### Phase 4 - drive-to-merge fan-out (WAVE 3)

PROBE for shepherd-driver (see "Composition with shepherd-driver").
On a probe MISS, STOP and ask the operator; do NOT inline the loop.

Re-render with `P4` `active`. Let `D` be the count of PRs to drive --
EVERY PR in the table that is not `triaged-deferred`: the in-flight
community PRs (`ORIGIN = community`, from Phase 2) PLUS the own fix PRs
(`ORIGIN = own-fix`, from Phase 3). Substitute `D` into the P4 label;
if `D = 0`, render P4 as `skipped`.

Print the `drive-<pr>` dispatch table (subagent_id -> PR number), then
spawn ONE shepherd-driver subagent per PR using
`../shepherd-driver/assets/shepherd-driver-prompt.md`. Each driver
runs in its OWN worktree (Worktree-isolation invariant): own-fix rows
REUSE the worktree their Phase 3 fix child recorded; community rows
get a fresh `git worktree add` + `gh pr checkout`, recorded in
`worktree`. Pass the inputs the prompt declares, reading each from the
row (never recall); `REPO_ROOT` is the row's worktree path. For rows
with `strategic_verdict = aligned-with-reservations`, ALSO pass
`PANEL_PRIOR = {"reservations": [<the strategic reservations as
{summary} objects>]}` so the driver surfaces them in the panel run and
the PR advisory comment.

If `D` is large, batch the spawns (e.g. groups of 3) to bound
nested-panel fan-out rather than launching all drivers at once.

Each driver owns the full convergence loop end-to-end and returns a
`completion_return` matching
`../shepherd-driver/assets/completion-schema.json` (status enum per
the Composition section). Schema-validate every return (retry-once; on
second malformed, mark the row `blocked` and continue). Write
`head_sha`, `mergeable`, `merge_state_status`, and `ci_status` from
the return into the table, and remove the `status/shepherding` label
from the driven issue (assignment stays). The orchestrator owns only
schema-validation, table update, and label cleanup -- it does NOT post
to any PR.

### Phase 5 - mergeability gate (WAVE 4)

Re-render with `WAVE4` `active`. Substitute `R` (ready-PR count)
and `C` (CONFLICTING-PR count) into the P5a / P5b labels. If
`R = 0`, skip Phase 5 entirely; if `C = 0`, render P5b as
`skipped`.

**Load `../shepherd-driver/references/mergeability-gate.md` when
entering this phase** -- it holds the binding step-by-step (probe CLI
flags, retry policy, four-way partition, trust-but-verify re-probe).
The contract summary:

- 5a (read-only): probe every Phase-4 ready PR via S7
  DETERMINISTIC TOOL BRIDGE (`gh pr view --json` with the S7
  mergeability fields -- full flag list in `mergeability-gate.md`).
  Skip `triaged-deferred` rows. Partition CLEAN / UNSTABLE / HAS_HOOKS
  (verified-ready) from BEHIND / DIRTY / CONFLICTING (route to 5b).
  BLOCKED is not a conflict.
- 5b (fan-out, one subagent per CONFLICTING PR): print dispatch
  table, spawn `resolve-conflicts-<pr>` subagents using
  `../shepherd-driver/assets/conflict-resolution-prompt.md`. Each owns
  its PR end-to-end: rebase, faithful conflict merge, lint silent,
  push with `--force-with-lease` (NEVER bare `--force`), re-probe,
  post the single resolution-confirmation comment. Returns are
  `conflict_resolution_return` matching
  `../shepherd-driver/assets/completion-schema.json`.
- 5c (read-only): trust-but-verify re-probe; partition into the
  schema's four `conflict_resolution_return` statuses; update the
  table.

### Phase 6 - final report

Re-render with every phase `done` (or `blocked` where the
human-escalation queue is non-empty). Render
`assets/final-report-template.md`: per-issue verdict, PR link,
post-gate status (the template's status set), and subagent session
refs. Phase-1.5-demoted rows land in the template's "Recommend close
as out-of-scope" partition, each citing the principle that fired.

Use clickable GitHub links (issue + pull URLs under
`github.com/microsoft/apm`) and `@<author>` profile links, not plain
issue numbers.

After the report renders, tear down ONLY the worktrees this run
created (`git worktree remove` each slug in the `worktree` column);
leave branches on origin for the open PRs.

## Bundled assets

This skill bundles ONLY the assets unique to its triage-and-batch
orchestration. Everything PR-drive related is owned by the composed
shepherd-driver sibling, loaded from `../shepherd-driver/`, not
duplicated here.

- `assets/verdict-schema.json` -- JSON schema for the TWO BBS-owned
  subagent return shapes (`triage_return`, `strategic_alignment_return`).
  Schema-validate every return (S4).
- `assets/ground-truth-table.md` -- canonical table template
  (`issue | verdict | pr | pr_in_flight | author | head_repo |
  head_branch | maintainer_can_modify | worktree | status |
  strategic_verdict | strategic_rationale | notes`).
- `assets/triage-prompt.md` -- WAVE 1 spawn body.
- `assets/strategic-alignment-prompt.md` -- WAVE 1.5 spawn body
  (loads `apm-ceo` persona + PRINCIPLES.md).
- `assets/fix-prompt.md` -- WAVE 2 greenfield-fix spawn body.
- `assets/final-report-template.md` -- user-facing report shape.
- `assets/progress-diagram.md` -- mermaid progress diagram, color
  contract, dispatch-table render rules (Phase 1, 1.5, 3, 4, 5b).
- `references/strategic-alignment-gate.md` -- Phase 1.5
  step-by-step (external-dep probes, fail-open semantics,
  deferred-PR strategic-rejection subagent). Load WHEN ENTERING
  PHASE 1.5.
- `references/invariants.md` -- full binding text of the 17
  architecture invariants. Load BEFORE PLANNING PHASE 0.

Composed from shepherd-driver (loaded by relative path, NOT bundled):
`shepherd-driver-prompt.md` (Phase 4 drive),
`conflict-resolution-prompt.md` (Phase 5b),
`completion-schema.json` (driver + resolution returns),
`references/mergeability-gate.md` (Phase 5 gate) -- all under
`../shepherd-driver/`.

## Operating contract for the orchestrator thread

The orchestrator loop, beyond the invariant anchors above:

- Before each phase: re-read `plan.md` ground-truth table. After each
  subagent return: schema-validate, update the table, write it back.
- Delegate every PR-side write to the responsible subagent; the
  orchestrator never posts to a PR and never flips merge state.
- Render the progress mermaid + live table at every phase boundary,
  and the dispatch table before every fan-out
  (`assets/progress-diagram.md`).

## Out of scope

- Authoring panel personas (lives in `apm-review-panel`).
- Computing coverage percentages (lives in test-coverage-expert
  persona, invoked via apm-review-panel).
- Single-PR review without a batch (use `apm-review-panel` directly).
- Driving a single PR to merge without a batch (use `shepherd-driver`
  directly).
- Auto-merge or auto-label. The orchestrator does not flip merge
  state; the maintainer ships.
