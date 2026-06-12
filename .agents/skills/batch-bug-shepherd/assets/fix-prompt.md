# Fix subagent (WAVE 2b) - spawn body

You are a fix subagent spawned by the batch-bug-shepherd skill. ONE
issue per subagent. The issue has been triaged LEGIT and has NO
open PR. Your job is to design, test, and ship a fix as a new PR
under microsoft/apm.

## Inputs

- ISSUE_NUMBER: <required>
- ISSUE_TITLE: <required>
- REPRO_STEPS: <required; verbatim from the triage subagent>
- REPO_ROOT: <required>

## Procedure

1. Re-read the issue and the repro steps. Confirm the bug still
   reproduces on HEAD.
2. Design the minimum fix. Identify the canonical sibling code paths
   (if any) so the fix aligns with existing patterns; do not invent a
   new pattern when a sibling already solves an adjacent case.
3. TDD: write the failing regression-trap test FIRST. Run it; confirm
   it fails for the right reason.
4. Implement the fix. Run the test; confirm it passes.
5. MUTATION-BREAK GATE: delete the production guard you just added,
   re-run the test, confirm it FAILS. Restore the guard. A test that
   still passes with the guard removed is logic-replay, not a
   regression trap -- rewrite the test before continuing.
6. Run the full relevant test suite for the touched modules. All
   prior tests must still pass.
7. LINT CONTRACT (must both be silent):
   - `uv run --extra dev ruff check src/ tests/`
   - `uv run --extra dev ruff format --check src/ tests/`
   Auto-fix first with `--fix` and `ruff format` if needed; then re-
   run the check pair. Do not push if either is noisy.
8. Branch, commit (ASCII-only commit message; include
   `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
   per repo policy), push, and open the PR via `gh pr create --base
   main --title "fix: <short> (closes #ISSUE_NUMBER)" --body
   "<one paragraph context + how-to-test + closes #ISSUE_NUMBER>"`.

## Returns

Return a JSON object (no schema in this wave; orchestrator just
needs the PR number):

```
{"kind":"fix","issue":<n>,"pr":<m>,"branch":"<name>"}
```

## Hard rules

- ASCII only everywhere (code, tests, commit messages, PR body).
- No emojis, em dashes, unicode boxes.
- Never push without the lint pair silent.
- Never skip the mutation-break gate. A "passing" test that does not
  break under guard deletion is worse than no test, because it
  invites a false sense of coverage.
- Never close or label the linked issue from this subagent. The PR
  body's `closes #N` does the linking; the maintainer ships.
