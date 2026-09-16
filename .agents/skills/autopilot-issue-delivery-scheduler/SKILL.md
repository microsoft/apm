---
name: autopilot-issue-delivery-scheduler
activation_card: on
description: >-
  Use this skill to queue maintainer-accepted microsoft/apm
  issues (`status/accepted`) and fan them out through an isolated
  pool (default 2) of autopilot-issue-delivery-worker sessions. Any accepted
  type is eligible. Advisory `triage/recommended` is not
  authorization. Does not triage. Does not review PRs. Works in
  a local session, Copilot App automation, Cloud Agent, Remote
  Agent, or Agentic Workflow.
---

# autopilot-issue-delivery-scheduler

User-facing ACCEPTED-ISSUE implementation queue. This skill
SELECTS authorized issues and THROTTLES fan-out. It does not
triage and does not review PRs.

Compose [autopilot-issue-delivery-worker](../autopilot-issue-delivery-worker/SKILL.md)
one issue per slot. Never borrow slots from
`autopilot-issue-triage-scheduler`,
`autopilot-pr-review-scheduler`, or
`autopilot-pr-triage-scheduler`.

## Activation card

`activation_card: on`. Before any queue read or spawn, emit
this Enter card with every field filled. Missing field -> stop.

```text
skill: autopilot-issue-delivery-scheduler
skill_path: <resolved directory of this SKILL.md>
mode: run
subject: microsoft/apm
path: delivery
intent: select accepted issues and fan out delivery workers
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

After the run, emit this Exit receipt:

```text
skill: autopilot-issue-delivery-scheduler
subject: microsoft/apm
path: delivery
write: off
queued: <integer>
spawned: <integer>
approved: n/a
```

## Invocation

Works in local sessions, Copilot App automations, Cloud Agent,
Remote Agent, and Agentic Workflows.

Resolve ORIGIN before any GitHub write:

- `unattended` -- workers still run; they must not assign.
- `actor-session` -- workers treat assignment as a hard gate
  before any implementation. Assignment is the public signal of
  which user is working the issue.

Ownership writes:

- Issue triage: none (no assignment needed)
- Code (accepted implementation): assign the implementing user (`@me`)
- PR review: request the reviewing user as reviewer, never assignee

INTENT for this scheduler is `implement` only as a parent signal
to workers. This scheduler itself never assigns, never requests
reviewers, and never writes human decision labels.

## Selector

- `all` (default) -- every open issue that already carries
  `status/accepted`, plus any issue the caller named as a
  bounded accept. Type does not matter (`type/bug`,
  `type/feature`, `type/automation`, docs, refactor). Escalate
  everything else. Do not run triage-panel from this scheduler.
- `bugs` -- same gate, plus `type/bug`. Worker uses LEGIT /
  UNCLEAR / FIXED-AT-HEAD plus PRINCIPLES.md alignment.

`type/bug` or `triage/recommended` without `status/accepted`
is not eligible unless the caller named a bounded accept.

## Selection

Build the queue from the caller list or `gh issue list` on
`status/accepted` (selector `bugs`: also require `type/bug`).

An issue is eligible for the queue only if it already carries
`status/accepted` or the caller named it as a bounded accept.
That membership is a communication signal, not implementation
permission. `triage/recommended` and legacy `status/triaged`
are advisory processing markers and are not authorization.
Workers re-check `scripts/governance/eligibility.cjs` from the
trusted default branch and require fresh responsible-human
confirmation before any mutate. ORIGIN `unattended` never
implements. Do not dispatch an unaccepted issue. Do not run
triage-panel to create any marker. Do not write `status/accepted`.

Skip locked, closed, and bot-authored issues unless the caller
named them. Deduplicate by number.

Skip `status/needs-design` unless the caller named the issue.
Skip `status/needs-triage` and `status/deferred` unless named.
Skip `status/shepherding` and `status/in-flight` unless named
(those belong to PR review or an in-flight owner). Skip issues
assigned to another user unless the caller named them; do not
steal in-progress work.

Do not open a second PR when one already addresses the issue;
return that PR number.

After a worker opens a PR, do NOT fill this pool with
`autopilot-pr-merge-worker`. Hand the PR number to the
caller for `autopilot-pr-review-scheduler`.

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
You are the sole table writer. Put selector and existing-PR
facts in `rationale`.

## Fan-out

Load [assets/fan-out-pool.md](assets/fan-out-pool.md). Default
`FANOUT_LIMIT=2` concurrent slots. Isolated to this run.
`FANOUT_LIMIT` is concurrency, not queue length. Drain the full
selected list; when a slot returns, fill it with the next item.

## Procedure

1. Probe worker-code on disk. Missing sibling -> stop.
2. Build the queue. Emit the mandatory queue table (labels +
   rationale) before any spawn.
3. Drain the selected list. Concurrent slots <= FANOUT_LIMIT.
   One issue per slot. When a slot returns, dispatch the next
   queued issue. Do not stop because the pool was full. Name each
   worker session
   `#<issue-number> autopilot-issue-delivery-worker <Issue Title>`.
4. Never auto-merge.
5. Print a final report from the table.

## Hard nos

- Do not share this pool.
- Do not dispatch the same issue to two slots.
- Do not dispatch an unaccepted issue.
- Do not dispatch an issue assigned to another user unless named.
- Do not triage or review PRs inside this scheduler.
- Do not contradict CODEOWNERS.
- ASCII only.
