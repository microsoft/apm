---
name: autopilot-pr-triage-scheduler
description: >-
  Queue open microsoft/apm pull requests (community fixes, PRs
  without an issue, or PRs that close an issue) and fan them out
  through an isolated pool (default 2) of
  autopilot-pr-triage-worker sessions. Advisory only. Never
  merges, never assigns, never requests reviewers, never runs
  apm-review-panel. Activate on a mixed PR list or queue-open when
  the job is PR triage. Works in a local session, Copilot App
  automation, Cloud Agent, Remote Agent, or Agentic Workflow.
---

# autopilot-pr-triage-scheduler

User-facing PR TRIAGE queue. This skill SELECTS pull requests and
THROTTLES fan-out. It classifies incoming PRs. It does not review
diffs, does not implement issues, and does not drive PRs to merge.

Compose [autopilot-pr-triage-worker](../autopilot-pr-triage-worker/SKILL.md)
one PR per slot. Never borrow slots from
`autopilot-issue-triage-scheduler`,
`autopilot-issue-delivery-scheduler`, or
`autopilot-pr-review-scheduler`.

## Invocation

Works in local sessions, Copilot App automations, Cloud Agent,
Remote Agent, and Agentic Workflows.

Resolve ORIGIN before any GitHub write. No assignment needed.
This scheduler and its slots never assign and never request
reviewers, in any ORIGIN.

INTENT is `triage`.

Ownership writes:

- Issue triage: none (no assignment needed)
- PR triage: none (no assignment needed)
- Code (accepted implementation): assign the implementing user
- PR review: request the reviewing user as reviewer, never assignee

## Selection (same helper as issue triage)

Canonical owner: this skill's `scripts/fetch_queue.py`,
`scripts/triage_state.py`, plus `assets/label-contract.json`.
Byte-identical to the issue-triage scheduler copy. Do not invent a
second filter. The worker does not list the queue.
`--kind pr` uses the same issue-shaped schema (`number` is the PR
number).

Modes:

- Named list or one PR number: explicit request.
- `queue-open` / no names: sweep.

`fetch_queue.py` owns list + eligibility (closed, merged, locked,
bot, empty, template-only; sweep-only draft and spam). Do not
comment on skips. `triage_state.py` owns completed-advice skip,
at most two per author, oldest first, cap 10. Explicit requests
bypass only spam and completed-advice.

A missing linked issue is not a skip. Community PRs without an
issue are in-scope; the worker advises whether an issue should
have existed.

```
python <this-skill>/scripts/fetch_queue.py --kind pr --mode sweep --repo microsoft/apm > batch.json
python <this-skill>/scripts/triage_state.py < batch.json
```

Named PR: `--mode dispatch --number N`. If `gh` is unavailable,
pass `--records-json` and `--labels-json`. A helper failure stops
the run.

Do not merge. Do not push. Do not fill issue-triage, delivery, or
PR-review slots from this pool. Do not comment on PRs. Do not add
or remove labels. `add_labels` from `triage_state.py` is a plan
for the worker, not a scheduler write.

## Fan-out

Load [assets/fan-out-pool.md](assets/fan-out-pool.md). Default
`FANOUT_LIMIT=2` concurrent slots. Isolated to this run.
`FANOUT_LIMIT` is concurrency, not queue length. Drain the full
selected list; when a slot returns, fill it with the next item.

## Procedure

1. Probe `scripts/fetch_queue.py`, `scripts/triage_state.py`, and
   the worker skill on disk. Missing -> stop.
2. Build the queue with the helpers. Persist a `plan.md` table
   for every selected number (number, slot, status, linked-issue).
   You are the sole table writer. Do not truncate the table to
   FANOUT_LIMIT.
3. Drain the selected list. Concurrent slots <= FANOUT_LIMIT.
   One PR per slot. Prefer a new session, else a sub-agent, else
   run the worker in this thread for that one PR, then the next.
   When a slot returns, dispatch the next queued PR. Do not stop
   because the pool was full. Each slot reads the complete PR
   conversation (and linked issue, if any) before advising.
4. Print a final report from the table.

## Hard nos

- Do not share this pool.
- Do not dispatch the same PR to two slots.
- Do not comment, label, close, merge, or assign. Workers own those
  writes even when summoned without this scheduler.
- Do not run `apm-review-panel` or `autopilot-pr-review-worker`.
- Do not contradict CODEOWNERS.
- ASCII only.
