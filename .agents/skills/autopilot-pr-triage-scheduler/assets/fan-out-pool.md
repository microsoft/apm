# Isolated fan-out pool (PR triage scheduler)

This pool belongs to THIS `autopilot-pr-triage-scheduler`
run only. Never share slots with
`autopilot-issue-triage-scheduler`,
`autopilot-issue-delivery-scheduler`, or
`autopilot-pr-review-scheduler`, or with another PR-triage
scheduler run.

## Limit

`FANOUT_LIMIT` defaults to 2. The caller may raise it. Never go
below 1. A full pool is not a reason to drop queue items; wait for
a slot.

## Fill order (each free slot)

1. Prefer a new session (Copilot App / Cloud / Remote) named
   `#<pr-number> pr-triage-worker` whose kickoff runs
   `autopilot-pr-triage-worker` on exactly one PR.
   Truncate the title if the harness caps length.
2. Else spawn a sub-agent (`task`) with the worker skill.
3. Else run the worker sequentially in this session.

## Dispatch rules

- One PR per slot. Never the same PR number in two slots.
- When a slot returns, take the next queued PR.
- ORIGIN is resolved in the worker session, not as a batch cheat.
- This pool never assigns, never requests reviewers, and never
  writes human decision labels.
