# Implement lens: type/bug

Coverage gate: REGRESSION TRAP + MUTATION-BREAK.

1. Reproduce the bug on HEAD inside your worktree. Capture the minimal
   reproduction the brief's `acceptance_tests` describe.
2. Write a FAILING regression test that fails because of the bug
   (red). This is the trap.
3. Implement the minimum fix so the trap passes (green).
4. MUTATION-BREAK gate: delete or invert the guard your fix added and
   confirm the trap FAILS without it; then restore the guard. A trap
   that still passes under guard deletion is worse than no test --
   redesign it. Record the mutation you applied as `coverage_gate`.
5. Confirm no other tests regressed:
   `uv run --extra dev pytest -q`.

Scope fence: fix ONLY the reported defect and its direct cause. A
broader refactor sparked by the fix is a `non_goals` violation --
defer it (the shepherd-driver fold layer will surface it as a panel
follow-up if it is in scope).
