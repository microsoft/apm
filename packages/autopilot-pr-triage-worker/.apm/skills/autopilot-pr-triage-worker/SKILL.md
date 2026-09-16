---
name: autopilot-pr-triage-worker
activation_card: on
description: >-
  Use this skill to triage ONE microsoft/apm pull request already
  selected by autopilot-pr-triage-scheduler. Covers community
  PRs that fix an issue and PRs opened without an issue. Return one
  advisory recommendation, never human approval, never a diff review,
  never merge. Not a queue manager and not autopilot-pr-review-worker.
---

# autopilot-pr-triage-worker

Advisory classification of ONE already-selected PR. Do not review
the diff as `autopilot-pr-review-worker` does. Do not drive merge.

## Activation card

`activation_card: on`. Before any PR read or GitHub write, emit
this Enter card with every field filled. Missing field -> stop.

```text
skill: autopilot-pr-triage-worker
skill_path: <resolved directory of this SKILL.md>
mode: run
subject: microsoft/apm#<pr-number>
path: triage
intent: advise one already-selected PR
origin: unattended | actor-session
write: on | off
repo: microsoft/apm
pr: <positive integer>
invocation: agentic-workflow | actor-session
```

Rules:

- `write` defaults to `on` when the caller omitted it.
- `write: off` returns the filled template only. Do not comment,
  add labels, or remove labels.
- `write: on` posts the one advisory comment and processing /
  classification labels. Never human decision labels. Never assign.
  Never request reviewers.
- `origin` fail-closed unknown -> `unattended`.
- One PR. Do not nest a scheduler path.

After the run, emit this Exit receipt:

```text
skill: autopilot-pr-triage-worker
subject: microsoft/apm#<pr-number>
path: triage
write: on | off
posted: yes | no
labels_applied: <comma list or none>
approved: n/a
```

Read `assets/label-contract.json` (same contract as issue triage)
before reasoning. Do not invent a second marker.

## Origin and writes

No assignment needed. Never request reviewers. Unattended and
actor-session are equally read-only for ownership.

This worker owns those writes even when summoned without a
scheduler. The scheduler must not comment or label.

Allowed writes: one advisory comment plus processing / optional
classification labels from the contract. Never write human
decision labels (`status/accepted`, `status/needs-design`,
`status/deferred`, `status/needs-triage`).

CODEOWNERS `reviewRequests` is runtime authority. Note owners in
the comment. Never add or remove review requests.

## Context (mandatory)

Before advising, paginate the complete PR conversation, reviews,
and review comments. If the body or commits reference `Fixes` /
`Closes` / `#N`, read that issue's complete conversation too.
No fresh advisory without that context.

If a prior triage comment's watermark still matches HEAD
conversation, no-op (do not post a duplicate).

## Procedure

1. Confirm the PR is the single target. Missing number -> stop.
2. Gather full context. Record whether a linked issue exists.
3. Fill [assets/pr-triage-template.md](assets/pr-triage-template.md).
   Recommendation is one of: `ready-for-review` | `needs-design` |
   `needs-issue` | `duplicate-of` | `decline-with-reason` |
   `auto-handle`.
4. `needs-issue` when a substantial change arrived without a
   maintainer-accepted issue and should have been discussed first.
   Small docs/typo/bugfix PRs may be `ready-for-review` without an
   issue.
5. `ready-for-review` is not merge approval and is not a request
   to run `autopilot-pr-review-scheduler`.
6. Post only if the comment would change. Add the contract
   processing marker. Do not remove `status/needs-triage`.

## Hard nos

- Do not merge, push, assign, or request reviewers.
- Do not run `autopilot-pr-review-worker` or `autopilot-pr-merge-worker`.
- Do not contradict CODEOWNERS.
- Do not treat labels or this comment as `status/accepted`.
- ASCII only.
