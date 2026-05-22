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

This skill is an A10 ORCHESTRATOR-SAGA over three fan-out waves
(triage, shepherd-or-fix, completion) with a persisted ground-truth
table between phases. It COMPOSES the
[apm-review-panel](../apm-review-panel/SKILL.md) skill -- it does NOT
re-implement panel review. Per-PR shepherding is delegated; per-issue
verification, PR-in-flight branching, fix dispatch, completion, and
the cross-session table are owned here.

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
- **Operator visibility is a contract, not a courtesy.** The
  orchestrator MUST render the progress mermaid diagram (with the
  current phase styled `active`) plus the live ground-truth table to
  chat at every phase boundary, and MUST print a dispatch table
  immediately before every fan-out spawn. The exact contract --
  color palette, node labels, when to render -- lives in
  `assets/progress-diagram.md`. The operator is steering a saga that
  takes 30+ minutes wall and dozens of parallel subagents; without
  the diagram they cannot tell `still working` from `stuck`. Skipping
  the visibility renders breaks the saga's human-in-the-loop contract.
- **Bias toward folding recommendations into the in-flight PR.**
  When `shepherd_return.recommended_followups[]` is non-empty,
  the default is FOLD-INTO-PR via the completion subagent, NOT
  defer to a tracking issue. The completion subagent (see
  `assets/completion-prompt.md` step 2) classifies each
  recommended item against explicit criteria: items that touch
  files already in the diff, single helper extractions, regression
  traps for the PR's own behavior, hermetic integration tests of
  the new surface, and adjacent doc / CHANGELOG patches all FOLD
  by default. Only genuinely separable work -- cross-cutting
  refactors, broad doc restructuring, new feature work,
  architectural additions -- becomes a tracking issue via
  `gh issue create`. The verdict mapping (`shepherd-prompt.md`
  step 4) makes `ship_with_followups` with 0 blocking findings
  emit `verdict: ready-to-merge` precisely so the completion
  subagent runs on the fold-in surface rather than blocking the
  PR for what the panel itself called non-blocking. The skill
  ships now; it does not ship "now plus a backlog of papercuts
  the maintainer will never get to".

## Composition with apm-review-panel

`apm-review-panel` is the shepherd primitive. This skill spawns it as
the body of every shepherd subagent. The spawn prompt instructs the
subagent to:

1. ACTIVATE: invoke the `apm-review-panel` skill by name (the harness
   resolves it from its skill registry). If the harness reports the
   skill is not available, abort with a clear error -- do NOT attempt
   a partial shepherd pass.
2. LOAD: treat the skill body as the working spec for the shepherd
   subagent.
3. RUN: execute the panel against the target PR per that skill's
   contract (8 specialist personas + CEO synthesizer, single
   recommendation comment).
4. RETURN: a structured verdict matching `assets/verdict-schema.json`
   (`ready-to-merge` | `needs-author-changes` | `reject`) plus the
   list of blocking-severity findings the completion subagent must
   address.

This is the only dependency between the two skills. The orchestrator
NEVER reaches into apm-review-panel internals; it consumes the comment
and the verdict.

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

### Phase 2 - PR-in-flight cross-reference

Re-render the progress diagram with `P1` `done` and `P2` `active`.
Substitute `L` (LEGIT row count) into the P2 label.

For every `LEGIT` row, run `gh pr list --search
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

Two parallel sub-waves, both fan-out:

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

Re-render the progress diagram with `P0..P3` `done` and `P4`
`active`. Substitute `F` (PRs needing follow-up work) into the P4
label. If `F = 0`, render P4 as `skipped`.

Print the dispatch table mapping each `completion-<pr>` subagent_id
to its target PR BEFORE spawning.

For each PR (both 3a-shepherded community PRs and 3b-fixed PRs that
need follow-ups), spawn one completion subagent with
`assets/completion-prompt.md`. Each completion subagent:

1. Reads the shepherd PR comment, `BLOCKING_FOLLOWUPS`, and
   `RECOMMENDED_FOLLOWUPS` (the latter is the input channel for
   the fold-in invariant; see `verdict-schema.json` shepherd_return
   shape).
2. CLASSIFIES every recommended item as FOLD (cheap + on-path;
   lands in this PR) or DEFER (separable; opens a tracking issue
   via `gh issue create`). Bias toward FOLD per the architecture
   invariant.
3. Resolves every blocking-severity follow-up FIRST. Common shapes:
   - Extract helpers; align with canonical sibling logic.
   - Add regression-trap tests (mutation-break gate enforced).
   - Fix merge conflicts; rebase if cleaner.
4. IMPLEMENTS every FOLD item, consulting the right panelist
   persona per `source_persona` (e.g. python-architect for code,
   test-coverage-expert for tests, supply-chain-security-expert
   for trust-boundary work). For non-trivial single items it MAY
   spawn a nested Task subagent to keep context isolated.
5. FILES every DEFER item as a tracking issue with rationale +
   PR backlink; records the issue numbers in
   `deferred_followups[]`.
6. Runs the lint contract. Both commands MUST be silent.
7. Pushes:
   - Tries `git push <author-fork> <branch>` first when
     `maintainerCanModify=true`.
   - On rejection or when the flag is false, opens a superseding
     PR under `microsoft/apm` via `git checkout -b
     supersede/<original-pr> && git cherry-pick ... && gh pr create
     --base main --title "..." --body "Supersedes #<n>; preserves
     authorship via commit trailers."`. Each commit carries
     `Co-authored-by: <original-author>` trailer.
   - Closes the superseded PR via `gh pr close <n> --comment
     "Superseded by #<m>. Thank you for the original work; the
     superseding PR preserves your authorship via commit trailers and
     resolves the panel follow-ups so we can land this promptly."`.
8. Waits for CI on the target PR. If green AND every blocking
   follow-up is addressed AND every recommended follow-up is
   either folded or filed as a tracking issue, posts ONE
   confirmation comment (template in
   `assets/final-report-template.md` -> "PR confirmation" block)
   listing folded items (with file:line citations) and deferred
   items (with tracking-issue numbers).
9. Cross-session-messages the orchestrator with the completion
   return JSON: `status: ready-to-merge`, plus `folded_followups`
   and `deferred_followups` arrays. On failure, stays in-session
   and surfaces the blocker for human review; does NOT message
   back as green.

### Phase 5 - final report

Re-render the progress diagram with every phase `done` (or
`blocked` where the human-escalation queue is non-empty). Print the
final ground-truth table below it.

Read the table one last time. Render `assets/final-report-template.md`
to the user: per-issue verdict, PR link, ready-to-merge status,
unresolved blockers (with the responsible subagent's session
reference), and any rows still `UNCLEAR` for human triage.

Use clickable GitHub links (`https://github.com/microsoft/apm/issues/<n>`
and `.../pull/<n>`) and `@<author>` references that resolve to
profile URLs in markdown-rendering chat clients. Plain text issue
numbers without links force the operator to copy-paste -- defeats
the purpose of the report.

## Bundled assets

- `assets/verdict-schema.json` -- JSON schema for triage, shepherd,
  and completion returns. Schema-validate every subagent return
  (S4 SCHEMA-VALIDATE).
- `assets/ground-truth-table.md` -- canonical table template.
  Columns: `issue | verdict | pr | pr_in_flight | author | status |
  notes`. Updated on every subagent return.
- `assets/triage-prompt.md` -- spawn body for WAVE 1 subagents.
- `assets/shepherd-prompt.md` -- spawn body for WAVE 2a subagents
  (loads apm-review-panel).
- `assets/fix-prompt.md` -- spawn body for WAVE 2b subagents.
- `assets/completion-prompt.md` -- spawn body for WAVE 3 subagents.
- `assets/final-report-template.md` -- the user-facing report shape
  AND the PR confirmation comment shape used by completion
  subagents.
- `assets/progress-diagram.md` -- the mermaid progress diagram, the
  color contract (pending / active / done / blocked / skipped), and
  the dispatch-table render rules. Re-rendered at every phase
  boundary.

## Operating contract for the orchestrator thread

- Before each phase: re-read `plan.md` ground-truth table. Do NOT
  rely on recall from earlier phases.
- After each subagent return: schema-validate, then update the
  table, then write it back to `plan.md`.
- Never post to a PR directly. Delegate every PR-side write to the
  subagent responsible for that PR.
- Never skip the cross-reference phase. The "duplicates community
  work" failure mode is more expensive than every other failure mode
  this skill defends against, combined.
- Honor the lint and encoding rules transitively: every spawn prompt
  reminds its subagent of both.
- Render the progress mermaid + the live ground-truth table to chat
  at every phase boundary, and the dispatch table before every
  fan-out wave. Skipping these renders is a contract violation, not
  a stylistic choice (`assets/progress-diagram.md`).

## Out of scope

- Authoring panel personas (lives in `apm-review-panel`).
- Computing coverage percentages (lives in test-coverage-expert
  persona, invoked via apm-review-panel).
- Single-PR review without a batch (use `apm-review-panel` directly).
- Auto-merge or auto-label. The orchestrator does not flip merge
  state; the maintainer ships.
