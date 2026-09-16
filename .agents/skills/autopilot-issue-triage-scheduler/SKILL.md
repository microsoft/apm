---
name: autopilot-issue-triage-scheduler
activation_card: on
description: >-
  Queue open microsoft/apm issues and fan them out through an
  isolated pool (default 2) of autopilot-issue-triage-worker
  sessions. Advisory only. Never implements, never assigns, never
  requests reviewers.
  Activate on a mixed issue list or queue-all when the job is
  triage. Works in a local session, Copilot App automation, Cloud
  Agent, Remote Agent, or Agentic Workflow.
---

# autopilot-issue-triage-scheduler

User-facing issue TRIAGE queue. This skill SELECTS issues and
THROTTLES fan-out. It does not implement and does not review PRs.

Compose [autopilot-issue-triage-worker](../autopilot-issue-triage-worker/SKILL.md)
one issue per slot. Never borrow slots from
`autopilot-issue-delivery-scheduler`,
`autopilot-pr-review-scheduler`, or
`autopilot-pr-triage-scheduler`.

## Activation card

`activation_card: on`. Before any queue read or spawn, emit
this Enter card with every field filled. Missing field -> stop.
Do not load Autogenesis path modules from this card.

```text
skill: autopilot-issue-triage-scheduler
skill_path: <resolved directory of this SKILL.md>
mode: run
subject: microsoft/apm
path: triage
intent: select issues and fan out triage workers
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

After the run, emit this Exit receipt:

```text
skill: autopilot-issue-triage-scheduler
subject: microsoft/apm
path: triage
write: off
queued: <integer>
spawned: <integer>
approved: n/a
```

## Invocation

Works in local sessions, Copilot App automations, Cloud Agent,
Remote Agent, and Agentic Workflows.

Resolve ORIGIN before any GitHub write. No assignment needed.
This scheduler and its slots never assign and never request
reviewers, in any ORIGIN.

INTENT is `triage`.

Ownership writes:

- Issue triage: none (no assignment needed)
- Code (accepted implementation): assign the implementing user
- PR review: request the reviewing user as reviewer, never assignee

## Selection (same contract as Triage Panel AW)

Canonical owner: this skill's `scripts/fetch_queue.py`,
`scripts/triage_state.py`, and `assets/label-contract.json`.
The PR-triage scheduler ships an identical copy. Do not invent a
second filter. The worker does not list the queue.

Modes (match `.github/workflows/triage-panel.md`):

- Named list or one issue number: explicit request (`dispatch` /
  `label-event`). `triage/requested` is the only request trigger.
  `status/needs-triage` is human decision state, not an event, and
  is not consumed.
- `queue-all` / no names: daily-style sweep.

`fetch_queue.py` owns list + eligibility (closed, locked, bot,
empty, template-only; sweep-only spam). Do not comment on skips.
`triage_state.py` owns completed-advice skip
(`processing.read_reviewed`), at most two per author, oldest first,
cap 10. Explicit requests bypass only spam and completed-advice.

Default (authenticated `gh`):

```
python <this-skill>/scripts/fetch_queue.py --kind issue --mode sweep --repo microsoft/apm > batch.json
python <this-skill>/scripts/triage_state.py < batch.json
```

Named issue: `--mode dispatch --number N`. If `gh` is unavailable,
pass `--records-json` and `--labels-json`. A helper failure stops
the run.

Do not implement. Do not open PRs. Do not fill delivery or review
slots from this pool. Do not comment on issues. Do not add or
remove labels. `add_labels` from `triage_state.py` is a plan for
the worker, not a scheduler write.

## Fan-out

Load [assets/fan-out-pool.md](assets/fan-out-pool.md). Default
`FANOUT_LIMIT=2` concurrent slots. Isolated to this run.
`FANOUT_LIMIT` is concurrency, not queue length. Drain the full
selected list; when a slot returns, fill it with the next item.

## Procedure

1. Probe `scripts/fetch_queue.py`, `scripts/triage_state.py`, and
   autopilot-issue-triage-worker on disk. Missing -> stop.
2. Build the queue with the helpers. Persist a `plan.md` table
   for every selected number (number, slot, status). You are the
   sole table writer. Do not truncate the table to FANOUT_LIMIT.
3. Drain the selected list. Concurrent slots <= FANOUT_LIMIT.
   One issue per slot. Prefer a new session, else a sub-agent,
   else run the worker in this thread for that one issue, then
   the next. When a slot returns, dispatch the next queued
   issue. Do not stop because the pool was full. Each slot reads
   the complete comment history before advising.
4. Optionally present ONE consolidated triage digest. Wait if the
   caller asked for a checkpoint. Never treat silence as accept.
5. Print a final report from the table.

## Hard nos

- Do not share this pool.
- Do not dispatch the same issue to two slots.
- Do not implement inside the scheduler thread.
- Do not comment, label, close, or assign. Workers own those writes
  even when summoned without this scheduler.
- Do not contradict CODEOWNERS.
- ASCII only.
