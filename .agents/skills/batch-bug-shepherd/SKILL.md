---
name: batch-bug-shepherd
description: >-
  Use this skill to drive a batch of suspected bugs in microsoft/apm
  from raw issue list to mergeable PR queue. Fan out one triage
  subagent per issue (LEGIT / UNCLEAR / FIXED-AT-HEAD), cross-reference
  legit issues against open PRs, then branch: in-flight community PR
  -> shepherd via the apm-review-panel skill; no PR -> fix session with
  TDD and a mutation-break gate. Dispatch one completion subagent per
  shepherd verdict to resolve panel follow-ups, push to the contributor
  fork (or open a superseding PR that preserves author authorship via
  commit trailers), and post one ready-to-merge confirmation. Maintain
  a single plan.md ground-truth table as canonical session state.
  Activate when the maintainer asks to triage a list of issues, sweep
  the bug queue or backlog, shepherd all bug-flagged issues this
  quarter, run a weekly sweep of community-reported issues, drive
  in-flight community PRs to merge, or work down community bug
  contributions -- even if "shepherd" or "batch" is not named.
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

### Phase 1 - triage fan-out (WAVE 1)

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

For every `LEGIT` row, run `gh pr list --search
"<issue-ref-or-keywords>" --state open --json
number,title,headRefName,author,maintainerCanModify`. Also inspect
each linked PR on the issue itself. Two outcomes per row:

- `pr_in_flight = false` -> route to FIX in phase 3.
- `pr_in_flight = true` -> capture PR number, author, fork URL,
  `maintainerCanModify` flag. Route to SHEPHERD in phase 3.

Update the table. This phase MUST complete before any phase-3 spawn.

### Phase 3 - shepherd-or-fix fan-out (WAVE 2)

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

For each PR (both 3a-shepherded community PRs and 3b-fixed PRs that
need follow-ups), spawn one completion subagent with
`assets/completion-prompt.md`. Each completion subagent:

1. Reads the shepherd comment (or, for own-fix PRs, its own
   self-review notes from the fix subagent return).
2. Resolves each blocking-severity follow-up. Common shapes:
   - Extract helpers; align with canonical sibling logic.
   - Add regression-trap tests (mutation-break gate enforced).
   - Fix merge conflicts; rebase if cleaner.
3. Runs the lint contract. Both commands MUST be silent.
4. Pushes:
   - Tries `git push <author-fork> <branch>` first when
     `maintainerCanModify=true`.
   - On rejection or when the flag is false, opens a superseding PR
     under `microsoft/apm` via `git checkout -b
     supersede/<original-pr> && git cherry-pick ... && gh pr create
     --base main --title "..." --body "Supersedes #<n>; preserves
     authorship via commit trailers."`. Each commit carries
     `Co-authored-by: <original-author>` trailer.
   - Closes the superseded PR via `gh pr close <n> --comment
     "Superseded by #<m>. Thank you for the original work; the
     superseding PR preserves your authorship via commit trailers and
     resolves the panel follow-ups so we can land this promptly."`.
5. Waits for CI on the target PR. If green AND every blocking
   follow-up is addressed, posts ONE confirmation comment (template
   in `assets/final-report-template.md` -> "PR confirmation" block)
   summarizing the changes and citing CI evidence.
6. Cross-session-messages the orchestrator with the PR number and
   `status: ready-to-merge`. On failure, stays in-session and
   surfaces the blocker for human review; does NOT message back as
   green.

### Phase 5 - final report

Read the table one last time. Render `assets/final-report-template.md`
to the user: per-issue verdict, PR link, ready-to-merge status,
unresolved blockers (with the responsible subagent's session
reference), and any rows still `UNCLEAR` for human triage.

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

## Out of scope

- Authoring panel personas (lives in `apm-review-panel`).
- Computing coverage percentages (lives in test-coverage-expert
  persona, invoked via apm-review-panel).
- Single-PR review without a batch (use `apm-review-panel` directly).
- Auto-merge or auto-label. The orchestrator does not flip merge
  state; the maintainer ships.
