# Isolated fan-out pool (PR review scheduler)

This pool belongs to THIS
`autopilot-pr-review-scheduler` run only. Never share
slots with `autopilot-issue-triage-scheduler`,
`autopilot-issue-delivery-scheduler`, or
`autopilot-pr-triage-scheduler`, or with another PR-review
scheduler run.

## Limit

`FANOUT_LIMIT` defaults to 2 concurrent slots. The caller may
raise it. Never go below 1. It is concurrency, not queue length.
A full pool is not a reason to drop queue items; wait for a
slot, then dispatch the next queued item until the selected list
is empty.

## Fill order (each free slot)

1. Prefer a new session (Copilot App / Cloud / Remote) whose kickoff
   runs `autopilot-pr-review-worker` on exactly one PR.
2. Else spawn a sub-agent (`task`) with that skill.
3. Else run sequentially in this session.
   Never spawn `autopilot-pr-merge-worker` from this pool.

## Dispatch rules

- One PR per slot. Never the same PR number in two slots.
- When a slot returns, take the next queued PR.
- ORIGIN is resolved in the worker session, not as a batch cheat.
- Assignment, labels, and CODEOWNERS follow the review / worker
  contract. Never contradict CODEOWNERS `reviewRequests`.
