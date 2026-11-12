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
one PR per slot (`INVOCATION_MODE=session-review`,
`panel-mode: full` for a first advisory). Never compose
`autopilot-pr-merge-worker`. Drive-to-merge is a different skill.

Never borrow slots from `autopilot-issue-triage-scheduler`,
`autopilot-issue-delivery-scheduler`, or
`autopilot-pr-triage-scheduler`.

## Activation card

`activation_card: on`. Before any queue read or spawn, emit
this Enter card with every field filled. Missing field -> stop.

```text
skill: autopilot-pr-review-scheduler
skill_path: <resolved directory of this SKILL.md>
mode: run
subject: microsoft/apm
path: review
intent: select accepted review PRs and fan out review workers
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
- `invocation` is the harness. Copilot App, local session, Cloud
  Agent, and Remote Agent are `actor-session`. `agentic-workflow`
  is only gh-aw / GitHub Actions. Do not copy `origin` into
  `invocation`.
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

`status/accepted` on the PR is a sweep source, not a request
trigger. A maintainer already accepted that PR. Union it with
the `panel-review` list. Do not treat `status/accepted` on a
linked issue as a sweep source (that would require listing every
open PR).

No accepted, no review. After building the union, drop any PR
that is not `status/accepted` on the PR or a same-repo linked
issue. Do not spawn it. Do not comment. Do not remove labels.
The review-worker, if already invoked, also stops with no
comment and may clear `panel-review`.

Modes:

- Named list or one PR number: explicit request. Honor those
  numbers even without `panel-review`.
- `queue-open` / no names: open PRs that currently have
  `panel-review` or `status/accepted` on the PR.

Never list all open PRs. Never fall back to an unfiltered
`gh pr list --state open`. Empty label queue -> empty table, stop.

Default (authenticated `gh`):

```
gh pr list --state open --label panel-review --json number,title,isDraft,labels,updatedAt
gh pr list --state open --label status/accepted --json number,title,isDraft,labels,updatedAt
```

Deduplicate by number. Skip drafts unless the caller named them.
Label sweep: oldest first, cap 10. Named list is not capped.

Do not invent a second trigger label.

## CODEOWNERS last-comment gate

Before any spawn, for each candidate PR (named list,
`panel-review` sweep, or `status/accepted` on the PR). This
gate does not bypass `status/accepted`. Named list and
`panel-review` do not bypass this gate.

1. Snapshot the CODEOWNERS set from current `reviewRequests`
   (users and teams). If empty, use `CODEOWNERS` for the changed
   paths. If still empty, skip this gate.
2. Paginate issue comments and submitted reviews to exhaustion.
   Ignore bots.
3. Last CODEOWNER comment = latest of those authored by the
   CODEOWNERS set.
4. If none: eligible. The worker still reads full context.
5. Read that comment as standing conditions (open an issue, link
   it, wait, change approach, add labels, or explicitly ask for a
   panel or further review). Evaluate whether those conditions are
   already met using later comments AND current labels on the PR
   and on same-repo linked issues. Do not treat "last word" as a
   stop when the asked work is done.
6. If the comment explicitly asks for a panel or further review,
   or its conditions are met: eligible. The comment is required
   context. Do not contradict it.
7. If conditions are not met: drop. Do not spawn. Do not comment.
   Do not change labels. Rationale:
   `CODEOWNERS last comment conditions unmet`.
8. If it is unclear whether conditions are met: fail closed.
   Drop with rationale:
   `CODEOWNERS last comment conditions unclear`.

## Queue table (mandatory)

Before any spawn, emit the keep-set and the drop-set in `plan.md`
and in the session report. Missing table, missing column, or blank
rationale -> stop. Do not spawn.

Keep-set (items this run will schedule). One row per item. Do not
truncate to FANOUT_LIMIT:

| number | kind | labels | rationale | slot |

Drop-set (considered, then not scheduled). `slot` is `-`:

| number | kind | labels | rationale | slot |

- `kind`: `issue` or `pr`
- `labels`: current GitHub labels, comma-separated; `none` if empty
- `rationale`: why queued or dropped (helper rule and labels)
- `slot`: 1-based spawn order, or `-` when dropped

Empty keep-set is success. Still emit the drop-set, or `none`.
You are the sole table writer. Name missing `panel-review`,
missing `status/accepted`, or a CODEOWNERS last-comment drop
in `rationale`.

## Fan-out

Load [assets/fan-out-pool.md](assets/fan-out-pool.md). Default
`FANOUT_LIMIT=2` concurrent slots. Isolated to this run.
`FANOUT_LIMIT` is concurrency, not queue length. Drain the full
selected list; when a slot returns, fill it with the next item.

## Procedure

1. Probe autopilot-pr-review-worker on disk. Missing sibling -> stop.
   Do not probe or spawn autopilot-pr-merge-worker.
2. Apply the CODEOWNERS last-comment gate. Emit the mandatory
   queue table (labels + rationale) before any spawn. Drain the
   keep-set. Concurrent slots <= FANOUT_LIMIT. One PR per slot.
   When a slot returns, dispatch the next queued PR. Do not stop
   because the pool was full.
3. Each slot reads the complete PR conversation, including the
   last CODEOWNER comment, and honors CODEOWNERS `reviewRequests`.
   Pass `panel-mode: full` on the worker activation card (first
   advisory). The worker may still lean or add ad-hoc personas.
4. Never auto-merge.

## Hard nos

- Do not share this pool.
- Do not dispatch the same PR to two slots.
- Do not comment, label, close, assign, or request reviewers.
  Reviewing sessions own those writes.
- Do not list all open PRs. `panel-review`, `status/accepted` on
  the PR, or a named list only.
- Do not open issues or greenfield PRs.
- Do not implement. Do not drive-to-merge.
- Do not compose `autopilot-pr-merge-worker`.
- Do not contradict CODEOWNERS.
- ASCII only.
