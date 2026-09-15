---
name: autopilot-issue-delivery-worker
description: >-
  Use this skill to implement ONE microsoft/apm issue already
  selected by autopilot-issue-delivery-scheduler. Queue signal is
  `status/accepted` or a named bounded accept; that is not
  permission. Requires fresh human-scope evidence from
  scripts/governance before any mutate. May reproduce a bug,
  implement the accepted change, and open a fix PR. Not a queue
  manager and not a triage scheduler. Works in the scheduler's
  session, a child session, Cloud Agent, Remote Agent, or Agentic
  Workflow.
---

# autopilot-issue-delivery-worker

Per-issue implementation worker. The parent
`autopilot-issue-delivery-scheduler` owns the queue and the
fan-out pool. You own exactly one `ISSUE_NUMBER`.

Do not pick more issues. Do not fill other slots.

## Inputs

- `ISSUE_NUMBER` -- required
- `SELECTOR` -- `all` (default) or `bugs`
- `REPO_ROOT` -- required
- `ORIGIN` -- if the parent passed it, honor it; else resolve

## ORIGIN and assignment

Resolve ORIGIN before any GitHub write:

1. Caller ORIGIN / INVOCATION_MODE
2. `GH_AW_*` or `GITHUB_ACTIONS` -> `unattended`
3. `gh api user --jq .login` succeeds -> `actor-session`
4. Else `unattended` (fail closed)

INTENT is `implement`.

When ORIGIN is `actor-session`, assignment is a hard gate. It
is the public signal of which user is working this issue.
Before reproduce, edits, or a PR:

1. Read current assignees.
2. If another user is assigned, escalate. Do not steal.
3. If unassigned, `gh issue edit --add-assignee @me` and
   verify the actor is listed.
4. If already assigned to `@me`, continue.

Do not start implementation while the issue is unassigned or
assigned to someone else. If a PR exists or is opened, assign
it the same way (`gh pr edit --add-assignee @me`). Do not
request that actor as a reviewer. Never alter CODEOWNERS
`reviewRequests`. If ORIGIN is `unattended` or unknown, skip
assignee writes. A failed required assignee write is `blocked`.

Never write human decision labels. Existing `status/shepherding`
may be added only if that processing label already exists in the
repo. Do not apply `status/accepted`.
Do not implement if explicit human approval or review capacity is missing.

## Gate (both selectors)

Refuse unless the issue already has `status/accepted` or the
parent recorded a bounded accept. That is the queue signal,
not permission. `triage/recommended` and `status/triaged` are
not authorization. Escalate; do not implement.

Selector `bugs` also requires `type/bug`. Selector `all` accepts
any type.

Then re-check human-scope evidence. Do not create another
parser or roster; `authority.cjs` alone interprets the record.
From `TRUSTED_GOVERNANCE_ROOT` (trusted default-branch copy,
never the issue worktree):

```
node scripts/governance/eligibility.cjs --help
node scripts/governance/eligibility.cjs --repo microsoft/apm --issue N --approval-url URL
```

Require `state: record-present` with
`authorizes_implementation: false`. That result is evidence,
never permission; current snapshots cannot detect deleted withdrawals.
Then require fresh explicit responsible-human
confirmation for this issue's bounded scope. ORIGIN
`unattended` never claims to have obtained it -- STOP and
return `blocked`. ORIGIN `actor-session` may proceed only
after the caller confirms in this session. Assignment,
labels, eligibility reports, and prior receipts are not that
confirmation. Any other state (`withdrawn`, `error`, missing
tool) -- STOP.

## Procedure

1. Do not run the triage scheduler. Triage belongs to
   `autopilot-issue-triage-scheduler`. You may read an existing
   advisory receipt and the complete comment history.
2. If ORIGIN is `actor-session`, pass the assignment hard gate
   first.
3. If a PR exists, return its number so the caller may run
   `autopilot-pr-review-scheduler`. Do not request
   yourself as reviewer. Do not start PR review from this worker.
4. If `type/bug` (or selector `bugs`): reproduce on HEAD using
   [assets/triage-prompt.md](assets/triage-prompt.md)
   (LEGIT / UNCLEAR / FIXED-AT-HEAD). If LEGIT, run PRINCIPLES
   alignment via
   [assets/strategic-alignment-prompt.md](assets/strategic-alignment-prompt.md).
   If greenfield and aligned: [assets/fix-prompt.md](assets/fix-prompt.md)
   (TDD + mutation-break), then open one PR.
5. Otherwise follow
   [assets/solution-pipeline-prompt.md](assets/solution-pipeline-prompt.md)
   and type-specific `assets/implement-*.md`. Open at most one PR.
   Author the body with pr-description-skill.

## Return

JSON with `issue`, `status` (`done` / `escalate` / `blocked` /
`pr-opened` / `pr-in-flight`), optional `pr`, and a one-line note.
Do not auto-merge.

ASCII only.
