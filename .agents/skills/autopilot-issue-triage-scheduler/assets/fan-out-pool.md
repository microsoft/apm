# Isolated fan-out pool (issue triage scheduler)

This pool belongs to THIS `autopilot-issue-triage-scheduler` run
only. Never share slots with `autopilot-issue-delivery-scheduler`,
`autopilot-pr-review-scheduler`, or
`autopilot-pr-triage-scheduler`, or with another
triage-scheduler run.

## Limit

`FANOUT_LIMIT` defaults to 2. The caller may raise it. Never go
below 1. A full pool is not a reason to drop queue items; wait for
a slot.

## Fill order (each free slot)

1. Prefer a new session (Copilot App / Cloud / Remote) whose kickoff
   runs `autopilot-issue-triage-worker` on exactly one issue.
2. Else spawn a sub-agent (`task`) with the worker skill.
3. Else run the worker sequentially in this session.

## Dispatch rules

- One issue per slot. Never the same issue number in two slots.
- When a slot returns, take the next queued issue.
- ORIGIN is resolved in the worker session, not as a batch cheat.
- This pool never assigns, never requests reviewers, and never
  writes human decision labels.
