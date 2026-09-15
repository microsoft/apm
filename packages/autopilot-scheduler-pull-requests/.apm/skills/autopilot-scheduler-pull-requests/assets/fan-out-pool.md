# Isolated fan-out pool (PR scheduler)

This pool belongs to THIS `autopilot-scheduler-pull-requests` run
only. Never share slots with `autopilot-scheduler-issues` or with
another PR-scheduler run.

## Limit

`FANOUT_LIMIT` defaults to 2. The caller may raise it. Never go
below 1. A full pool is not a reason to drop queue items; wait for
a slot.

## Fill order (each free slot)

1. Prefer a new session (Copilot App / Cloud / Remote) whose kickoff
   runs `autopilot-pr-merge-worker` on exactly one PR.
2. Else spawn a sub-agent (`task`) with the worker skill.
3. Else run the worker sequentially in this session.

## Dispatch rules

- One PR per slot. Never the same PR number in two slots.
- When a slot returns, take the next queued PR.
- ORIGIN is resolved in the worker session, not as a batch cheat.
- Assignment, labels, and CODEOWNERS follow the worker contract.
