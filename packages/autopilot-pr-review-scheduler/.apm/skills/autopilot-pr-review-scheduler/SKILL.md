---
name: autopilot-pr-review-scheduler
description: >-
  Queue open microsoft/apm pull requests and fan them out through
  an isolated pool (default 2) of review sessions. Default is
  standalone apm-review-panel (advisory). Pass composed-implementation-review
  only when the caller asked to drive an existing PR. Works in a
  local session, Copilot App automation, Cloud Agent, Remote Agent,
  or Agentic Workflow. Does not triage issues or open greenfield PRs.
---

# autopilot-pr-review-scheduler

User-facing PR REVIEW queue. This skill SELECTS pull requests and
THROTTLES fan-out. It does not triage issues and does not open
greenfield PRs.

Default compose [apm-review-panel](../apm-review-panel/SKILL.md)
one PR per slot (`INVOCATION_MODE=session-review`).

Compose [autopilot-pr-review-worker](../autopilot-pr-review-worker/SKILL.md)
only when the caller asked for drive-to-merge
(`INVOCATION_MODE=composed-implementation-review`).

Never borrow slots from `autopilot-issue-triage-scheduler`,
`autopilot-issue-delivery-scheduler`, or
`autopilot-pr-triage-scheduler`.

## Invocation

Works in local sessions, Copilot App automations, Cloud Agent,
Remote Agent, and Agentic Workflows.

Resolve ORIGIN before any GitHub write:

- `unattended` -- never assign, never request reviewers.
- `actor-session` -- standalone review must request `@me` as a
  supplemental reviewer (`gh pr edit --add-reviewer @me`) and
  verify. That request is the public signal of which user is
  running the review. Never assign the PR. Skip only on
  `self-review-red-flag` (operator is the PR author). Composed
  review never requests the implementer.

This scheduler itself never assigns issues or PRs and never
writes human decision labels.

Ownership writes:

- Issue triage: none (no assignment needed)
- Code (accepted implementation): assign the implementing user
- PR review: request the reviewing user as reviewer, never assignee

## Selection

Build the queue from the caller list, a label, or
`gh pr list --state open`. Deduplicate by number. Skip drafts if
the caller did not include them.

## Fan-out

Load [assets/fan-out-pool.md](assets/fan-out-pool.md). Default
`FANOUT_LIMIT=2`. Isolated to this run.

## Procedure

1. Probe apm-review-panel (and worker-pull-request if composed)
   on disk. Missing sibling -> stop.
2. Fill the pool. One PR per slot. Persist a `plan.md` table
   (number, slot, status, head). You are the sole table writer.
3. Each slot reads the complete PR conversation and honors
   CODEOWNERS `reviewRequests`.
4. Never auto-merge.

## Hard nos

- Do not share this pool.
- Do not dispatch the same PR to two slots.
- Do not assign issues or PRs.
- Do not skip the `@me` reviewer request on actor-session
  standalone review except `self-review-red-flag`.
- Do not open issues or greenfield PRs.
- Do not contradict CODEOWNERS.
- ASCII only.
