---
name: autopilot-pr-triage-scheduler
activation_card: on
description: >-
  Queue open microsoft/apm pull requests (community fixes, PRs
  without an issue, or PRs that close an issue) and fan them out
  through an isolated pool (default 2) of
  autopilot-pr-triage-worker sessions. Advisory only. Never
  merges, never assigns, never requests reviewers, never runs
  autopilot-pr-review-worker. Activate on a mixed PR list or queue-open when
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

## Activation card

`activation_card: on`. Before any queue read or spawn, emit
this Enter card with every field filled. Missing field -> stop.

```text
skill: autopilot-pr-triage-scheduler
skill_path: <resolved directory of this SKILL.md>
mode: run
subject: microsoft/apm
path: triage
intent: select PRs and fan out triage workers
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
skill: autopilot-pr-triage-scheduler
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
empty, template-only; sweep-only draft and spam). Bot-authored
issues and PRs stay eligible. Sweep list
excludes `processing.read_reviewed` (`triage/recommended`,
`status/triaged`) at GitHub so already-advised open PRs are not
downloaded. Do not comment on skips. `triage_state.py` owns
completed-advice skip, at most two per author, oldest first,
cap 10. Explicit requests bypass only spam and completed-advice.

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
Completed-advice excluded at fetch is not a drop-set row.
You are the sole table writer. Put linked-issue facts in
`rationale`.

## Fan-out

Load [assets/fan-out-pool.md](assets/fan-out-pool.md). Default
`FANOUT_LIMIT=2` concurrent slots. Isolated to this run.
`FANOUT_LIMIT` is concurrency, not queue length. Drain the full
selected list; when a slot returns, fill it with the next item.

## Procedure

1. Probe `scripts/fetch_queue.py`, `scripts/triage_state.py`, and
   the worker skill on disk. Missing -> stop.
2. Build the queue with the helpers. Emit the mandatory queue
   table (labels + rationale) before any spawn.
3. Drain the selected list. Concurrent slots <= FANOUT_LIMIT.
   One PR per slot. Prefer a new session, else a sub-agent, else
   run the worker in this thread for that one PR, then the next.
   When a slot returns, dispatch the next queued PR. Do not stop
   because the pool was full. Each slot reads the complete PR
   conversation (and linked issue, if any) before advising.
   Pass `debug: off` unless this run's caller set `debug: on`.
   Pass `comment_via: autopilot-comment`. Worker kickoff must
   load `autopilot-comment` before any GitHub comment. Workers
   must not call `gh pr comment`.
4. Print a final report from the table.

## Hard nos

- Do not share this pool.
- Do not dispatch the same PR to two slots.
- Do not comment, label, close, merge, or assign. Workers own those
  writes even when summoned without this scheduler.
- Do not run `autopilot-pr-review-worker` or `autopilot-pr-merge-worker`.
- Do not contradict CODEOWNERS.
- ASCII only.
