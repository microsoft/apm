---
name: batch-bug-shepherd
description: >-
  Use this skill to drive a batch of suspected bugs in microsoft/apm
  from raw issue list to mergeable PR queue. Fan out one triage
  subagent per issue (LEGIT / UNCLEAR / FIXED-AT-HEAD), cross-reference
  legit issues against open PRs, then branch: in-flight community PR
  -> shepherd via the apm-review-panel skill; no PR -> fix session with
  TDD and a mutation-break gate. Dispatch one completion subagent per
  shepherd verdict to resolve panel follow-ups, FOLD non-blocking
  recommendations into the same PR by default (DEFER cross-cutting
  items to tracking issues), push to the contributor fork (preserving
  author via commit trailers), and post one ready-to-merge
  confirmation. Maintain a single plan.md ground-truth table as
  canonical session state. Activate when the maintainer asks to triage
  issues, sweep the bug queue, shepherd bug-flagged issues, run a
  weekly community sweep, or drive in-flight community PRs to merge
  -- even if "shepherd" or "batch" is not named.
---

# batch-bug-shepherd - Outer-loop bug-queue orchestrator

This skill is an A10 ORCHESTRATOR-SAGA over five fan-out waves
(triage, strategic-alignment, shepherd-or-fix, completion,
conflict-resolution) with a persisted ground-truth table between
phases. It COMPOSES the
[apm-review-panel](../apm-review-panel/SKILL.md) skill -- it does NOT
re-implement panel review. It also COMPOSES the `apm-ceo` persona
(host-repo agent at `.apm/agents/apm-ceo.agent.md`) for the
strategic-alignment gate, which checks every LEGIT bug against
`PRINCIPLES.md` before allowing shepherd / fix work to proceed.
Per-PR shepherding is delegated; per-issue verification, strategic
alignment, PR-in-flight branching, fix dispatch, completion,
post-wave mergeability re-probe, and the cross-session table are
owned here.

The skill is ADVISORY at the panel layer and EXECUTIVE at the
orchestrator layer: it WILL push commits, open PRs, post comments,
close superseded PRs. Every consequential write goes through a
deterministic CLI (`gh`, `git`, `uv run ruff`) wrapped in plan +
execute + verify (A9 SUPERVISED EXECUTION).

## Architecture invariants

- **Fan-out, not serial.** Triage, shepherd, fix, and completion all
  run as parallel child threads via the runtime's `task` affordance.
  A single-loop variant of this skill is an anti-pattern -- it
  collapses the context-isolation win.
- **Verify before fix.** No fix subagent is dispatched until the
  issue is reproduced on HEAD (verdict `LEGIT`). `UNCLEAR` issues
  are surfaced for human triage; `FIXED-AT-HEAD` issues are
  recommended for close.
- **PR-in-flight detection is mandatory.** Before dispatching ANY
  fix, the orchestrator runs `gh pr list --search "<issue-ref>"` (and
  scans linked PRs on the issue) for every legit issue. Skipping
  this step risks duplicating community work, which is the worst
  failure mode this skill defends against.
- **Shepherd before complete.** When a community PR exists, the
  apm-review-panel verdict comment IS the work definition for the
  completion subagent. Completion does not freelance: it reads the
  shepherd comment, addresses each blocking-severity finding, and
  stops.
- **Mutation-break gate.** A regression-trap test is REAL only when
  deleting the production guard makes it FAIL. Tests that pass with
  the guard deleted are logic-replay, not regression traps. The
  completion subagent MUST run the mutation-break check before
  declaring the follow-up resolved (see
  `assets/completion-prompt.md`).
- **Superseding-PR fallback.** When push to the contributor fork
  fails (no `maintainerCanModify`, branch protection, or fork
  deleted), open a new PR under `microsoft/apm` that PRESERVES
  AUTHOR AUTHORSHIP via `git commit --author="<author>"` or
  cherry-pick + `Co-authored-by:` trailer. Close the original PR
  with a courteous handoff comment referencing the superseding PR.
- **Single-writer interlock per artifact.** Each apm-review-panel run
  posts exactly ONE comment (the panel's own contract). Each
  completion subagent posts exactly ONE confirmation comment after CI
  is green. The orchestrator never posts to a PR directly -- it
  delegates to the relevant subagent.
- **ASCII only.** All artifacts (table, comments, commit messages,
  templates) use printable ASCII. No emojis, no em dashes, no
  unicode box-drawing. Windows cp1252 terminals will UnicodeEncodeError
  on anything else.
- **Lint contract is the push gate.** Before any `git push`, the
  completion subagent runs the canonical pair:
  `uv run --extra dev ruff check src/ tests/ && uv run --extra dev ruff format --check src/ tests/`
  and both MUST be silent. See `.github/instructions/linting.instructions.md`.
- **Ground-truth table is the single source of truth.** One markdown
  table in the session's plan.md, rewritten on every subagent return.
  Schema in `assets/ground-truth-table.md`. Re-read it at the start
  of every wave (B4 PLAN MEMENTO + B8 ATTENTION ANCHOR).
- **Cross-session message reports only on green.** A completion
  subagent reports back to the orchestrator (via the runtime's
  cross-session-message affordance, or by writing a status line to
  plan.md if cross-session-message is unavailable) ONLY when CI is
  green and all blocking follow-ups landed. Failures stay in the
  subagent's session until resolved or escalated to a human.
- **Operator visibility is a contract, not a courtesy.** At every
  phase boundary the orchestrator MUST render the progress mermaid
  diagram (current phase `active`) + the live ground-truth table
  to chat, AND print a dispatch table immediately before every
  fan-out spawn. The full color contract, render rules, and
  dispatch-table format live in `assets/progress-diagram.md`. Saga
  takes 30+ minutes wall and dozens of parallel subagents; without
  the diagram the operator cannot tell `still working` from
  `stuck`.
- **Mergeability is post-wave truth, not pre-wave assumption.** A
  PR that Phase 4 marked ready-to-merge can stop being mergeable
  the moment the maintainer lands another PR onto main. The table
  is not allowed to claim `ready-to-merge` without a post-wave
  `gh pr view --json mergeStateStatus` re-probe. Phase 5 enforces
  this: every ready PR is re-probed; CONFLICTING ones go through a
  one-subagent-per-PR rebase + faithful conflict resolution +
  `--force-with-lease` push + re-probe; non-pushable forks
  (`maintainerCanModify=false`) surface as
  `requires-author-action`. Bare `--force` is prohibited. See
  `references/mergeability-gate.md`.
- **Two-comment-per-PR cap.** Across the entire saga, a single PR
  receives at most TWO orchestrator-controlled comments: the Phase
  4 completion-confirmation comment, and the Phase 5b
  resolution-confirmation comment (only when conflicts were
  resolved). The apm-review-panel comment posted in Phase 3 is the
  panel's own contract and does not count against this cap. No
  third comment from any phase under any circumstance.
- **Bias toward folding recommendations into the in-flight PR.**
  When `shepherd_return.recommended_followups[]` is non-empty, the
  default is FOLD-INTO-PR via the completion subagent, NOT defer
  to a tracking issue. The completion subagent (see
  `assets/completion-prompt.md` step 2) classifies each item FOLD
  vs DEFER with explicit criteria and biases toward FOLD on close
  calls. Only genuinely separable work -- cross-cutting refactors,
  broad doc restructuring, new feature work -- becomes a tracking
  issue. The verdict mapping makes `ship_with_followups` with 0
  blocking findings emit `verdict: ready-to-merge` so completion
  runs on the fold-in surface. Ships now, not "now plus a backlog
  of papercuts".
- **Strategic-alignment gate before shepherd work.** After Phase 1
  and BEFORE Phase 2, every LEGIT row passes through Phase 1.5:
  one `apm-ceo` subagent per row inspects the bug against
  `PRINCIPLES.md` (rejection contract) + `MANIFESTO.md`. Rows
  demoted to `out-of-scope` / `wrong-direction` SKIP Phase 2/3/4/5
  and surface in Phase 6 under "Recommend close as out-of-scope".
  The gate FAILS OPEN to `aligned` on subagent malformed-x2 or
  non-citable principle; it ABORTS only when `apm-ceo.agent.md` or
  `PRINCIPLES.md` itself is missing. Silently demoting under
  infrastructure failure would hide real defects. See
  `references/strategic-alignment-gate.md`.

## Composition with apm-review-panel

`apm-review-panel` is the shepherd primitive. This skill spawns it
as the body of every shepherd subagent. The spawn prompt ACTIVATES
the panel skill by name, runs it against the captured PR per the
panel's own contract (8 specialist personas + CEO synthesizer,
single recommendation comment), and RETURNS a verdict matching
`assets/verdict-schema.json` (`ready-to-merge` |
`needs-author-changes` | `reject`) plus blocking-severity findings
for the completion subagent. If the harness reports the panel skill
is unavailable, abort with a clear error.

This is the only dependency between the two skills. The
orchestrator NEVER reaches into apm-review-panel internals.

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
number,title,headRefName,author,maintainerCanModify`. Also inspect
each linked PR on the issue itself. Two outcomes per row:

- `pr_in_flight = false` -> route to FIX in phase 3.
- `pr_in_flight = true` -> capture PR number, author, fork URL,
  `maintainerCanModify` flag. Route to SHEPHERD in phase 3.

Update the table. This phase MUST complete before any phase-3 spawn.

### Phase 3 - shepherd-or-fix fan-out (WAVE 2)

Re-render the progress diagram with `P0..P2` `done` and the `WAVE2`
subgraph `active`. Substitute `k` and `m` into the P3a / P3b
labels. If `m = 0`, render P3b as `skipped` (dashed border).

Print TWO dispatch tables -- one for sub-wave 3a (shepherd-<pr>
subagent_ids -> PR numbers) and one for sub-wave 3b (fix-<issue>
subagent_ids -> issue numbers) -- BEFORE spawning either sub-wave.

Two parallel sub-waves, both fan-out. BOTH sub-waves filter out
any row with status `triaged-deferred` (strategically demoted by
Phase 1.5).

**Sub-wave 3a -- SHEPHERD.** For each PR-in-flight row, spawn a child
thread with `assets/shepherd-prompt.md` (which is a thin wrapper that
loads apm-review-panel and runs the panel against the captured PR).
Returns: verdict + comment URL. The panel writes ONE PR comment per
its own contract; the orchestrator does not post to that PR.

**Sub-wave 3b -- FIX.** For each `LEGIT && !pr_in_flight` row, spawn
a child thread with `assets/fix-prompt.md`. The fix subagent:
- Writes failing tests FIRST (TDD).
- Implements the minimum fix.
- Runs the mutation-break gate (delete the new guard, confirm tests
  FAIL).
- Runs the lint contract.
- Opens a PR under `microsoft/apm` referencing the issue.
- Returns PR number.

Update the table with PR numbers and shepherd verdicts. Hold until
every spawn returns.

### Phase 4 - completion fan-out (WAVE 3)

Re-render with `P4` `active`. Substitute `F` into the P4 label;
if `F = 0`, render P4 as `skipped`.

Print the `completion-<pr>` dispatch table, then for each PR
needing follow-ups (EXCLUDING `triaged-deferred` rows) spawn one
completion subagent with `assets/completion-prompt.md`. The full
procedure (CLASSIFY, resolve blockers FIRST, implement FOLD items
consulting the right panelist persona, file DEFER items via `gh
issue create`, lint silent, push-or-supersede, wait for CI, post
ONE confirmation comment) lives in the spawn body. For rows with
`strategic_verdict = aligned-with-reservations`, the subagent MUST
surface the reservations in its confirmation-comment prose. The
orchestrator owns only schema-validation and table update.

### Phase 5 - mergeability gate (WAVE 4)

Re-render with `WAVE4` `active`. Substitute `R` (ready-PR count)
and `C` (CONFLICTING-PR count) into the P5a / P5b labels. If
`R = 0`, skip Phase 5 entirely; if `C = 0`, render P5b as
`skipped`.

**Load `references/mergeability-gate.md` when entering this
phase** -- it holds the binding step-by-step (probe CLI flags,
retry policy, four-way partition, trust-but-verify re-probe). The
contract summary:

- 5a (read-only): probe every Phase-4 ready PR via S7
  DETERMINISTIC TOOL BRIDGE (`gh pr view --json
  mergeStateStatus,mergeable,maintainerCanModify,headRepository,
  headRepositoryOwner,headRefName`). Skip `triaged-deferred` rows.
  Partition CLEAN / UNSTABLE / HAS_HOOKS (verified-ready) from
  BEHIND / DIRTY / CONFLICTING (route to 5b). BLOCKED is not a
  conflict.
- 5b (fan-out, one subagent per CONFLICTING PR): print dispatch
  table, spawn `resolve-conflicts-<pr>` subagents using
  `assets/conflict-resolution-prompt.md`. Each owns its PR
  end-to-end: rebase, faithful conflict merge, lint silent, push
  with `--force-with-lease` (NEVER bare `--force`), re-probe, post
  the single resolution-confirmation comment.
- 5c (read-only): trust-but-verify re-probe; partition into the
  schema's four `conflict_resolution_return` statuses; update the
  table.

### Phase 6 - final report

Re-render with every phase `done` (or `blocked` where the
human-escalation queue is non-empty). Render
`assets/final-report-template.md`: per-issue verdict, PR link,
post-gate status (one of ready-to-merge-verified,
requires-author-action, requires-human-judgment, resolution-failed,
superseded, blocked, unclear, strategically-deferred), with
subagent session refs. The template includes a "Recommend close as
out-of-scope" partition for rows demoted by Phase 1.5, each citing
the principle that fired.

Use clickable GitHub links (`https://github.com/microsoft/apm/issues/<n>`
and `.../pull/<n>`) and `@<author>` profile links. Plain text
issue numbers defeat the purpose.

## Bundled assets

- `assets/verdict-schema.json` -- JSON schema for all five subagent
  return shapes. Schema-validate every return (S4).
- `assets/ground-truth-table.md` -- canonical table template
  (`issue | verdict | pr | pr_in_flight | author | status |
  strategic_verdict | strategic_rationale | notes`).
- `assets/triage-prompt.md` -- WAVE 1 spawn body.
- `assets/strategic-alignment-prompt.md` -- WAVE 1.5 spawn body
  (loads `apm-ceo` persona + PRINCIPLES.md).
- `assets/shepherd-prompt.md` -- WAVE 2a spawn body (loads
  apm-review-panel).
- `assets/fix-prompt.md` -- WAVE 2b spawn body.
- `assets/completion-prompt.md` -- WAVE 3 spawn body.
- `assets/conflict-resolution-prompt.md` -- WAVE 4 spawn body
  (rebase, faithful merge, `--force-with-lease`, re-probe, single
  resolution-confirmation comment).
- `assets/final-report-template.md` -- user-facing report shape +
  PR confirmation comment + resolution-confirmation comment.
- `assets/progress-diagram.md` -- mermaid progress diagram, color
  contract, dispatch-table render rules (Phase 1, 1.5, 3a, 3b, 4,
  5b).
- `references/strategic-alignment-gate.md` -- Phase 1.5
  step-by-step (external-dep probes, fail-open semantics,
  deferred-PR strategic-rejection subagent). Load WHEN ENTERING
  PHASE 1.5.
- `references/mergeability-gate.md` -- Phase 5 step-by-step (probe
  CLI, retry policy, trust-but-verify re-probe, four-way
  partition). Load WHEN ENTERING PHASE 5.

## Operating contract for the orchestrator thread

- Before each phase: re-read `plan.md` ground-truth table.
- After each subagent return: schema-validate, update the table,
  write it back to `plan.md`.
- Never post to a PR directly; delegate every PR-side write to the
  responsible subagent.
- Never skip the cross-reference phase. Duplicating community work
  is the most expensive failure mode this skill defends against.
- Honor the lint and encoding rules transitively (every spawn
  prompt reminds its subagent of both).
- Render the progress mermaid + live ground-truth table at every
  phase boundary, and the dispatch table before every fan-out wave
  (`assets/progress-diagram.md`).

## Out of scope

- Authoring panel personas (lives in `apm-review-panel`).
- Computing coverage percentages (lives in test-coverage-expert
  persona, invoked via apm-review-panel).
- Single-PR review without a batch (use `apm-review-panel` directly).
- Auto-merge or auto-label. The orchestrator does not flip merge
  state; the maintainer ships.
