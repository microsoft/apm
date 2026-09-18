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
json: off | on
debug: off | on
repo: microsoft/apm
pr: <positive integer>
invocation: agentic-workflow | actor-session
```

Rules:

- `write` defaults to `on` when the caller omitted it.
- `write: off` returns the filled template only. Do not comment,
  add labels, or remove labels.
- `write: on` posts one GitHub comment via `autopilot-comment`
  and processing / classification labels. Never assign. Never
  request reviewers. Do not call `gh pr comment` yourself.
  Do not write `status/accepted`, `status/needs-design`, or
  `status/needs-triage`. Write `status/deferred` only when this
  PR is not labelled `status/accepted` and has no same-repo
  linked issue labelled `status/accepted`.
- `json` defaults to `off` when omitted or unknown. Omitted `json`
  is not a missing-field stop.
- `json: off` -> no machine JSON receipt.
- `json: on` -> fill JSON only as an internal payload (session
  file or parent Exit). Never post it on GitHub.
- `debug` defaults to `off` when omitted or unknown. Omitted `debug`
  is not a missing-field stop.
- `debug: off` -> pass `public_body` as the Suggested PR comment
  (unwrap the markdown fence) to `autopilot-comment`. Do not
  pass recommendation, linked issue, classification, or next
  action headings as `public_body`.
- `debug: on` -> pass `debug_body` as the filled template
  without a JSON fence. `autopilot-comment` prefixes
  `[i] Skill debug is on.`
- Load `autopilot-comment` before any GitHub comment write.
  `source_skill: autopilot-pr-triage-worker`. Missing card ->
  stop.
- `origin` fail-closed unknown -> `unattended`.
- One PR. Do not nest a scheduler path.

After the run, emit this Exit receipt:

```text
skill: autopilot-pr-triage-worker
subject: microsoft/apm#<pr-number>
path: triage
write: on | off
json: off | on
debug: off | on
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
classification labels from the contract. Never write
`status/accepted`, `status/needs-design`, or
`status/needs-triage`.

Auto-defer: if this PR is not labelled `status/accepted` and
has no same-repo linked issue labelled `status/accepted`, add
`status/deferred` (`write: on` only).
Do not overwrite `status/accepted` already on the PR. Linked
means `Fixes` / `Closes` / `#N` in the body or commits, or
GitHub `closingIssuesReferences`, same repository. Any one
accepted linked issue or `status/accepted` on the PR blocks
auto-defer.

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
2. Gather full context. Record whether this PR is labelled
   `status/accepted`, and linked same-repo issues and whether
   any carries `status/accepted`.
3. Fill [assets/pr-triage-template.md](assets/pr-triage-template.md).
   Recommendation is one of: `ready-for-review` | `needs-design` |
   `needs-issue` | `duplicate-of` | `decline-with-reason` |
   `auto-handle`.
4. Neither this PR nor a linked same-repo issue is
   `status/accepted` -> recommend `needs-issue`,
   add `status/deferred` when `write: on`, and thank the author.
   Invite them to open an issue for maintainer review and
   acceptance first, per CONTRIBUTING.md ("Start with an issue,
   not an implementation."). Link
   https://github.com/microsoft/apm/blob/main/CONTRIBUTING.md
5. `ready-for-review` when this PR or a linked same-repo issue is
   `status/accepted`. It is not merge approval and is not a
   request to run `autopilot-pr-review-scheduler`.
6. Post only if the comment would change. Probe
   `$REPO_ROOT/.agents/skills/autopilot-comment/SKILL.md` (or the
   sibling package). Missing -> stop. Do not post. Activate
   `autopilot-comment` (`source_skill:
   autopilot-pr-triage-worker`). Do not call `gh pr comment`
   yourself. `debug: off`: Suggested PR comment body as
   `public_body` (not the filled template). `debug: on`: filled
   template as `debug_body`. Missing footer after that write
   is a failed post -- patch via `autopilot-comment`
   (`intent: patch`). Add the contract processing marker.
   Do not remove `status/needs-triage`.

## Hard nos

- Do not merge, push, assign, or request reviewers.
- Do not run `autopilot-pr-review-worker` or `autopilot-pr-merge-worker`.
- Do not contradict CODEOWNERS.
- Do not treat labels or this comment as `status/accepted`.
- Do not write `status/deferred` when a linked issue is
  `status/accepted` or the PR already has `status/accepted`.
- ASCII only.
