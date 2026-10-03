# Isolated fan-out pool (issue scheduler)

This pool belongs to THIS `autopilot-issue-delivery-scheduler` run
only. Never share slots with `autopilot-issue-triage-scheduler`,
`autopilot-pr-review-scheduler`, or
`autopilot-pr-triage-scheduler`, or with another
issue-scheduler run.

## Limit

`FANOUT_LIMIT` defaults to 2 concurrent slots. The caller may
raise it. Never go below 1. It is concurrency, not queue length.
A full pool is not a reason to drop queue items; wait for a
slot, then dispatch the next queued item until the selected list
is empty.

## Fill order (each free slot)

1. Prefer a new session (Copilot App / Cloud / Remote) named
   `Issue delivery #<issue-number>` whose kickoff runs
   `autopilot-issue-delivery-worker` on exactly one issue.
   Do not invent a nickname. Do not append the GitHub title.
2. Else spawn a sub-agent (`task`) with the worker skill. Use the
   same name string for the agent `name`.
3. Else run the worker sequentially in this session.

## Dispatch rules

- One issue per slot. Never the same issue number in two slots.
- When a slot returns, take the next queued issue.
- ORIGIN is resolved in the worker session, not as a batch cheat.
- Assignment, labels, and CODEOWNERS follow the worker contract.
- Do not fill a slot with PR review. After a PR opens, leave it
  for `autopilot-pr-review-scheduler`.
