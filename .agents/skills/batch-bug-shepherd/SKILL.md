---
name: batch-bug-shepherd
description: >-
  Use this skill to drive a batch of suspected bugs in microsoft/apm
  from raw issue list to mergeable PR queue. Fan out one triage
  subagent per issue (LEGIT / UNCLEAR / FIXED-AT-HEAD), gate every
  legit bug against PRINCIPLES.md via an apm-ceo strategic-alignment
  pass, cross-reference legit issues against open PRs, then open a fix
  PR (TDD + mutation-break gate) for greenfield bugs after a
  fresh human issue-scope checkpoint. Drive approved PRs
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
which gives advisory alignment against `PRINCIPLES.md`, never human
authorization. Per-PR shepherding is delegated to
shepherd-driver; per-issue verification, strategic alignment,
PR-in-flight branching, greenfield fix dispatch, post-wave
mergeability re-probe, and the cross-session table are owned here.

The skill is ADVISORY at the panel layer and EXECUTIVE at the
orchestrator layer: it WILL push commits, open PRs, post comments,
close superseded PRs ONLY within fresh human-confirmed issue scope.
Every consequential write goes through a
deterministic CLI (`gh`, `git`, `uv run ruff`) wrapped in plan +
execute + verify (A9 SUPERVISED EXECUTION).

## Architecture invariants

**Load `references/invariants.md` before planning Phase 0.** Its 18
binding rules, human checkpoint, and owned-marker procedure govern every
wave; these anchors do not replace that reference:

- Parallel triage/alignment/fix/drive; isolated worktree per mutating child.
- Reproduce before fixing; `UNCLEAR` -> human, `FIXED-AT-HEAD` -> recommend close.
- Cross-reference existing PRs before fixes; ONE driver owns each complete PR loop.
- Mutation-break tests; canonical-owner classification and dual guardrails.
- Preserve authorship when superseding; only the driver writes to a PR.
- Printable ASCII; canonical lint contract before every push.
- One plan.md table: reload at boundaries, rewrite on every return.
- Report completion only on green; unresolved failures escalate.
- Progress diagram and live table at boundaries; dispatch table before fan-out.
- Re-probe mergeability; conflict pushes use only `--force-with-lease`.
- Two-comment cap plus the idempotent panel surface; no third comment.
- Fold in-scope follow-ups; track only genuinely separable work.
- Alignment advice cannot authorize work; malformed/unavailable advice blocks.

Before ANY fix, drive, or conflict-resolution implementation, apply
the **Human scope checkpoint** in `references/invariants.md`. Discovery
and advice may proceed read-only; an existing PR, label, or CEO verdict
does not permit mutations.

## Composition with shepherd-driver

The same-repo LOCAL SIBLING `shepherd-driver` is declared in `apm.yml`.
Its `assets/shepherd-driver-prompt.md` owns the complete per-PR loop,
including its transitive apm-review-panel dependency. Returns follow
`assets/completion-schema.json`: `ready-to-merge`,
`advisory-with-deferred`, `superseded`, or `blocked`. Terminal evidence
also passes `scripts/owner_touch_gate.py`.

Phase 5 loads the sibling's `references/mergeability-gate.md` and
delegates to `assets/conflict-resolution-prompt.md`. Do not inline either
loop. Before the drive wave, perform this real dependency probe:

```
test -f ../shepherd-driver/assets/shepherd-driver-prompt.md \
  && test -f ../shepherd-driver/assets/completion-schema.json \
  && test -f ../shepherd-driver/scripts/owner_touch_gate.py \
  && echo "shepherd-driver present" \
  || echo "MISSING shepherd-driver - stop and ask the operator"
```

On a probe MISS, STOP and ask the operator to restore the sibling.
Use only its declared prompt, schema, and owner-gate interfaces; the
driver probes its own panel dependency and returns `blocked` on a miss.

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
- BOTH `gh issue list --repo microsoft/apm --label type/bug --state open --json number,title,labels,body`
  AND `gh issue list --repo microsoft/apm --label bug --state open --json number,title,labels,body`
- plus `gh issue list --state open --search "is:open no:label"` filtered
  by suspicion keywords (`error`, `crash`, `broken`, `regression`,
  `unexpected`, `traceback`, `does not work`, `cannot`, `fails`).

Union the canonical and legacy bug queries plus the suspicion fallback;
deduplicate by issue number before triage. Paginate each query to exhaustion
(do not mistake the CLI default limit for a complete queue). Read failures
stop discovery; labels select candidates, never authorize work.

Initialize the ground-truth table (`assets/ground-truth-table.md`)
with one row per candidate. Print a brief plan to the user:
candidate count, expected wave shape, and the disciplines that will
be enforced (mutation-break, ASCII, lint). Ask for confirmation only
if `sweep-all` produced more than 20 candidates -- otherwise proceed
with read-only triage, not implementation permission.

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
blocked-on-failure semantics, deferred-PR advisory subagent).

Probe `.apm/agents/apm-ceo.agent.md` and `PRINCIPLES.md`. Either
missing -> ABORT. Print the dispatch table for the
`ceo-align-<issue>` subagents, then spawn `L` parallel threads with
`assets/strategic-alignment-prompt.md`. Returns are
`strategic_alignment_return` JSON (verdict in `aligned` |
`aligned-with-reservations` | `out-of-scope` | `wrong-direction`).
Schema-validate per retry-once; on second malformed, route as
`blocked` with the diagnostic for human review, never `aligned`.

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

### Phase 2.5 - responsible-human issue-scope checkpoint

Apply `references/invariants.md` -> **Human scope checkpoint** to each
issue, including community PRs already in flight. Persist the nominated
approval URL, evidence state, exact human-confirmed scope and done-when,
exclusions, review contact, and fresh confirmation reference in the row's
session receipt. Missing evidence or confirmation leaves that row `blocked`;
do not dispatch fix/drive children or claim an owned shepherd marker.

### Phase 3 - greenfield fix fan-out (WAVE 2)

Re-render the progress diagram with `P0..P2` `done` and `P3` `active`.
Substitute `m` (greenfield count) into the P3 label. If `m = 0`,
render P3 as `skipped` (dashed border) and pass straight to Phase 4.

This wave is FIX-ONLY. In-flight community PRs do NOT pass through
here -- they go directly to the Phase 4 drive wave. Filter out any row
with status `triaged-deferred` (strategically demoted by Phase 1.5).
Also exclude `blocked` rows and rows without the Phase 2.5 checkpoint.

Print the `fix-<issue>` dispatch table (subagent_id -> issue number)
BEFORE spawning. For each `LEGIT && !pr_in_flight` row, provision one
git worktree (`git worktree add <path> origin/main`), record its slug
in the row's `worktree` column, and spawn a child thread with
`assets/fix-prompt.md` passing the worktree as `REPO_ROOT` and the
Phase 2.5 receipt as `HUMAN_SCOPE_RECEIPT`. The child rechecks scope
before editing, then follows that prompt's TDD/mutation/owner/lint gates.

Validate each return against `assets/verdict-schema.json` -> `fix_return`;
inspect `status` before reading `pr` or `branch`. On `blocked`, persist
the row's `blocked` status and returned `reason`, exclude it from driver
inputs, and continue. Malformed or wrong-issue returns also block.
Only `pr-opened` returns supply the Phase 4 driver inputs:
`PR_NUMBER` (pr), `AUTHOR` = the maintainer's own gh handle (these are
own PRs), `HEAD_REPO = microsoft/apm` (same-repo head),
`HEAD_BRANCH` (branch), `MAINTAINER_CAN_MODIFY = true`, and
`ORIGIN = own-fix`. Write `head_repo`, `head_branch`,
`maintainer_can_modify` into the row. Hold until every spawn returns.

### Phase 4 - drive-to-merge fan-out (WAVE 3)

PROBE for shepherd-driver (see "Composition with shepherd-driver").
On a probe MISS, STOP and ask the operator; do NOT inline the loop.

Re-render with `P4` `active`. Let `D` be the count of PRs to drive --
ONLY PRs with a current Phase 2.5 checkpoint, neither `blocked` nor
`triaged-deferred`: the in-flight
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
row (never recall); `REPO_ROOT` is the row's worktree path. Also
pass the human checkpoint receipt as a binding constraint: no drive,
fold, push, or superseding PR outside its scope; return `blocked` if
confirmation is missing or scope changes. For rows
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
second malformed, mark the row `blocked` and continue).

For a terminal `ready-to-merge` or `advisory-with-deferred` return,
persist the returned JSON in the session state, derive `BASE_SHA` with
`git -C <row-worktree> merge-base <returned-head-sha> origin/main`,
then independently run:

```
uv run python <row-worktree>/.agents/skills/shepherd-driver/scripts/owner_touch_gate.py verify \
  --repo-root <row-worktree> --base $BASE_SHA \
  --head <returned-head-sha> --completion <session-return-json>
```

Do not update the table or labels until schema AND semantic
verification pass. A non-zero verifier result gets the same retry-once
treatment as malformed schema; on a second failure mark the row
`blocked` with the diagnostic. This parent re-probe prevents a child
from bypassing deterministic owner detection or presenting stale
functional evidence.

After both gates pass, write `head_sha`, `mergeable`,
`merge_state_status`, and `ci_status` from the return into the table,
and remove `status/shepherding` ONLY under the owned-marker rule in
`references/invariants.md` (assignment stays). The orchestrator owns only validation, table
update, and label cleanup -- it does NOT post to any PR.

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
  only after rechecking the human checkpoint for this implementation;
  missing/currently uncertain scope blocks the row. Pass its receipt to the
  conflict-resolution child. Then print dispatch
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

- `assets/verdict-schema.json` -- triage, strategic-alignment, and
  discriminated fix-success/blocked return contracts.
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
  step-by-step (external-dep probes, blocked-on-failure semantics,
  deferred-PR strategic-rejection subagent). Load WHEN ENTERING
  PHASE 1.5.
- `references/invariants.md` -- full binding text of the 18
  architecture invariants. Load BEFORE PLANNING PHASE 0.

Composed from shepherd-driver (loaded by relative path, NOT bundled):
`shepherd-driver-prompt.md` (Phase 4 drive),
`conflict-resolution-prompt.md` (Phase 5b),
`completion-schema.json` (driver + resolution returns),
`scripts/owner_touch_gate.py` (terminal functional-evidence gate),
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
