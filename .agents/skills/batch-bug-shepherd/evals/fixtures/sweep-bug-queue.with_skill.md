# Orchestrator session: with batch-bug-shepherd skill loaded

User: sweep the bug queue and drive whatever's mergeable to a
ready-to-ship state.

Orchestrator (activates batch-bug-shepherd skill by name; loads its
SKILL.md as the working spec).

## Phase 0 -- scope resolution

Running candidate enumeration:

```
gh issue list --label bug --state open --json number,title,labels,body
gh issue list --state open --search "is:open no:label" \
  --json number,title,body
```

Resolved 25 candidates (22 bug-labeled + 3 untyped issues matching
suspicion keywords).

Count exceeds the sweep-all threshold of 20. ASKING FOR
CONFIRMATION before fan-out:

> 25 candidates resolved. Plan: 25 parallel triage subagents ->
> cross-reference open PRs for every LEGIT row -> shepherd-or-fix
> fan-out -> completion fan-out -> final report. Disciplines:
> mutation-break, ASCII, lint contract, single-writer per PR
> comment. Ground-truth table will live in plan.md. Proceed?

User: confirmed.

Initializing ground-truth table (`assets/ground-truth-table.md`)
with 25 rows:

| issue | verdict | pr | pr_in_flight | author | status | notes |
|-------|---------|----|----|--------|--------|-------|
| #281  | pending |    | unknown | -- | pending-triage | CLI traceback on missing apm.yml |

(24 more rows, identical shape)

## Phase 1 -- triage fan-out (25 parallel subagents)

Spawning 25 triage subagents in parallel via the runtime's `task`
affordance. Reloading the ground-truth table at this phase boundary.

Returns received: 12 LEGIT, 5 UNCLEAR, 8 FIXED-AT-HEAD. Table
updated.

## Phase 2 -- PR-in-flight cross-reference

Running `gh pr list --search "<issue-ref>" --state open` for each of
the 12 LEGIT rows. 4 LEGIT issues have in-flight community PRs; 8
LEGIT issues do NOT. Table updated.

## Phase 3 -- shepherd-or-fix fan-out

Reloading the ground-truth table.

Sub-wave 3a (SHEPHERD): 4 spawns, one per in-flight PR. Each
invokes apm-review-panel by name.

Sub-wave 3b (FIX): 8 spawns, each writes the failing test FIRST,
runs the mutation-break gate, runs the lint contract
(`ruff check src/ tests/` and `ruff format --check src/ tests/`
both silent), and opens a PR.

## Phase 4 -- completion fan-out

Reloading the ground-truth table.

12 completion subagents resolve blocking follow-ups, push, and
cross-session-message the orchestrator only when CI is green.

## Phase 5 -- final report

Reading the final table; rendering `assets/final-report-template.md`:

- 11 ready-to-merge (PR links + CI evidence)
- 1 still in-session (blocked on flaky CI)
- 8 FIXED-AT-HEAD (recommend close)
- 5 UNCLEAR (surfaced for human triage with repro notes)

Single-writer interlock honored on every PR. Lint contract honored
on every push.
