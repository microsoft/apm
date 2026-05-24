# Orchestrator session: with batch-bug-shepherd skill loaded

User: triage these bugs: #311 (CLI crash on empty config), #312
(auth: PAT lookup intermittent), #313 (deprecated --token-source
still in help text).

Orchestrator (activates batch-bug-shepherd skill by name; loads its
SKILL.md as the working spec).

## Phase 0 -- scope resolution

Three explicit issues, no sweep-all expansion needed. Below threshold
(20), proceeding without confirmation.

Ground-truth table written to plan.md:

| issue | verdict | pr | pr_in_flight | author | status | notes |
|-------|---------|----|----|--------|--------|-------|
| #311  | pending |    | unknown | -- | pending-triage | CLI crash on empty config |
| #312  | pending |    | unknown | -- | pending-triage | auth PAT lookup intermittent |
| #313  | pending |    | unknown | -- | pending-triage | --token-source help text |

## Phase 1 -- triage fan-out (3 parallel subagents)

Spawning 3 triage subagents in parallel, one per candidate, each
loading `assets/triage-prompt.md`. Subagent verdicts return as JSON
matching `verdict-schema.json`.

Returns:
- #311 -> LEGIT (reproduced via `apm init --config /dev/null`).
- #312 -> LEGIT (reproduced intermittently; race in AuthResolver
  cache).
- #313 -> FIXED-AT-HEAD; recommend close referencing #1402.

Table updated.

## Phase 2 -- PR-in-flight cross-reference

Running `gh pr list --search "#311" --state open` and equivalent for
#312. Skipping #313 (FIXED-AT-HEAD).

Results:
- #311 -> no in-flight PR.
- #312 -> PR #1428 in flight (community contributor; head
  contoso:fix/auth-cache-race; maintainerCanModify=true).

Table updated. PHASE 2 COMPLETE; gating phase 3.

## Phase 3 -- shepherd-or-fix fan-out (2 parallel sub-waves)

Sub-wave 3a (SHEPHERD): 1 spawn for PR #1428. Spawn loads
`assets/shepherd-prompt.md`. The shepherd subagent ACTIVATES the
apm-review-panel skill by name; the panel runs against PR #1428 per
its own contract (single PR comment, panelist fan-out, CEO
synthesis). Returns: verdict `needs-author-changes` + 2
blocking-severity follow-ups.

Sub-wave 3b (FIX): 1 spawn for issue #311. Spawn loads
`assets/fix-prompt.md`. The fix subagent writes the failing test
FIRST (TDD), implements the minimum fix, runs the mutation-break
gate (deletes the new guard -> test FAILS as required), runs the
lint contract (`ruff check src/ tests/` and `ruff format --check
src/ tests/` both silent), and opens PR #1437 referencing #311.

Table updated.

## Phase 4 -- completion fan-out

1 completion subagent for #312 (resolve 2 blocking follow-ups,
re-run lint, push to contoso fork, post one confirmation comment
when CI green).

1 completion subagent for #311 (self-review path; lint already
silent from the fix subagent; watch CI; post confirmation comment
when green).

Cross-session-message ONLY on green; failures stay in-session.

## Phase 5 -- final report

Reading the final table; rendering `assets/final-report-template.md`:

- #311: LEGIT; PR #1437 (own-fix); status ready-to-merge.
- #312: LEGIT; PR #1428 (community); status ready-to-merge after
  follow-ups landed.
- #313: FIXED-AT-HEAD; recommend close referencing #1402.

The orchestrator never posted to any PR directly; every PR-side
write was delegated to the responsible subagent. Single-writer
interlock honored. Lint contract honored on every push.
Mutation-break gate honored on the regression-trap test.
