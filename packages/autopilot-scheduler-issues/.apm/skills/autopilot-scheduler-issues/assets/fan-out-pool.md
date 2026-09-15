# Isolated fan-out pool (issue scheduler)

This pool belongs to THIS `autopilot-scheduler-issues` run only.
Never share slots with `autopilot-scheduler-pull-requests` or with
another issue-scheduler run.

## Limit

`FANOUT_LIMIT` defaults to 2. The caller may raise it. Never go
below 1. A full pool is not a reason to drop queue items; wait for
a slot.

## Fill order (each free slot)

1. Prefer a new session (Copilot App / Cloud / Remote) whose kickoff
   runs `autopilot-issue-delivery-worker` on exactly one issue.
2. Else spawn a sub-agent (`task`) with the worker skill.
3. Else run the worker sequentially in this session.

## Dispatch rules

- One issue per slot. Never the same issue number in two slots.
- When a slot returns, take the next queued issue.
- ORIGIN is resolved in the worker session, not as a batch cheat.
- Assignment, labels, and CODEOWNERS follow the worker contract.
