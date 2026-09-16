---
name: autopilot-pr-review-scheduler
activation_card: on
description: >-
  Queue microsoft/apm pull requests labelled `panel-review` (or an
  explicit named list) and fan them out through an isolated pool
  (default 2) of autopilot-pr-review-worker sessions. Advisory only.
  Never implements. Never composes autopilot-pr-merge-worker. Never
  review every open PR. Works in a local session, Copilot App
  automation, Cloud Agent, Remote Agent, or Agentic Workflow. Does
  not triage issues or open greenfield PRs.
---

# autopilot-pr-review-scheduler

User-facing PR REVIEW queue. This skill SELECTS pull requests and
THROTTLES fan-out. It does not triage issues, does not implement,
and does not open greenfield PRs.

Compose [autopilot-pr-review-worker](../autopilot-pr-review-worker/SKILL.md)
one PR per slot (`INVOCATION_MODE=session-review`). Never compose
`autopilot-pr-merge-worker`. Drive-to-merge is a different skill.

Never borrow slots from `autopilot-issue-triage-scheduler`,
`autopilot-issue-delivery-scheduler`, or
`autopilot-pr-triage-scheduler`.

## Activation card

`activation_card: on`. Before any queue read or spawn, emit
this Enter card with every field filled. Missing field -> stop.
Do not load Autogenesis path modules from this card.

```text
skill: autopilot-pr-review-scheduler
skill_path: <resolved directory of this SKILL.md>
mode: run
subject: microsoft/apm
path: review
intent: select accepted panel-review PRs and fan out review workers
origin: unattended | actor-session
write: off
repo: microsoft/apm
fanout_limit: <positive integer>
invocation: agentic-workflow | actor-session
```

Rules:

- `write` is always `off`. This scheduler never comments,
  labels, assigns, or requests reviewers.
- `write: on` -> stop.
- `origin` fail-closed unknown -> `unattended`.
- One queue. Do not nest another scheduler path.
- Do not compose `autopilot-pr-merge-worker`.

After the run, emit this Exit receipt:

```text
skill: autopilot-pr-review-scheduler
subject: microsoft/apm
path: review
write: off
queued: <integer>
spawned: <integer>
approved: n/a
```

## Invocation

Works in local sessions, Copilot App automations, Cloud Agent,
Remote Agent, and Agentic Workflows.

Resolve ORIGIN before spawning slots so each reviewing session
can apply ownership writes. This scheduler never comments,
labels, assigns, or requests reviewers.

- `unattended` -- slots never assign, never request reviewers.
- `actor-session` -- `autopilot-pr-review-worker` requests `@me`
  as a supplemental reviewer. Never assign the PR. Skip only on
  `self-review-red-flag` (operator is the PR author).

Ownership writes:

- Issue triage: none (no assignment needed)
- Code (accepted implementation): assign the implementing user
- PR review: the reviewing session requests the reviewing user as
  reviewer, never assignee. Scheduler does not perform that write.

## Selection

`panel-review` is the only request trigger. It matches
`.github/workflows/pr-review-panel.md`. It is not a human decision
label. Re-apply it (remove + add) for a fresh review after the
panel clears it.

`status/accepted` is the human action flag (on this PR or a
same-repo linked issue). No accepted, no review. After building
the `panel-review` list, drop any PR that is not accepted. Do
not spawn it. Do not comment. Do not remove labels. The
review-worker, if already invoked, also stops with no comment
and may clear `panel-review`.

Modes:

- Named list or one PR number: explicit request. Honor those
  numbers even without `panel-review`.
- `queue-open` / no names: only open PRs that currently have
  `panel-review`.

Never list all open PRs. Never fall back to an unfiltered
`gh pr list --state open`. Empty label queue -> empty table, stop.

Default (authenticated `gh`):

```
gh pr list --state open --label panel-review --json number,title,isDraft,labels,updatedAt
```

Deduplicate by number. Skip drafts unless the caller named them.
Label sweep: oldest first, cap 10. Named list is not capped.

Do not invent a second trigger label.

## Fan-out

Load [assets/fan-out-pool.md](assets/fan-out-pool.md). Default
`FANOUT_LIMIT=2` concurrent slots. Isolated to this run.
`FANOUT_LIMIT` is concurrency, not queue length. Drain the full
selected list; when a slot returns, fill it with the next item.

## Procedure

1. Probe autopilot-pr-review-worker on disk. Missing sibling -> stop.
   Do not probe or spawn autopilot-pr-merge-worker.
2. Drain the selected list. Concurrent slots <= FANOUT_LIMIT.
   One PR per slot. Persist a `plan.md` table for every selected
   number (number, slot, status, head). You are the sole table
   writer. Do not truncate the table to FANOUT_LIMIT. When a slot
   returns, dispatch the next queued PR. Do not stop because the
   pool was full.
3. Each slot reads the complete PR conversation and honors
   CODEOWNERS `reviewRequests`.
4. Never auto-merge.

## Hard nos

- Do not share this pool.
- Do not dispatch the same PR to two slots.
- Do not comment, label, close, assign, or request reviewers.
  Reviewing sessions own those writes.
- Do not list all open PRs. `panel-review` or a named list only.
- Do not open issues or greenfield PRs.
- Do not implement. Do not drive-to-merge.
- Do not compose `autopilot-pr-merge-worker`.
- Do not contradict CODEOWNERS.
- ASCII only.
