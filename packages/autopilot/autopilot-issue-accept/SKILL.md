---
name: autopilot-issue-accept
activation_card: on
description: >-
  Use this skill to accept ONE microsoft/apm issue with a declared
  scope. The canvas Accept button, or a maintainer asking to accept
  an issue, starts it. Draft only from the latest triage proposed
  scope, get exact-text approval in this chat from a GOVERNANCE
  roster human, post that comment as that human, then add
  status/accepted. Do not invent scope, do not accept pull
  requests, and do not implement the issue.
---

# autopilot-issue-accept

Accept one issue. You own the scope comment and `status/accepted`.
The canvas must not add that label. A later delivery worker may
trust the comment because a rostered human approved the exact text
before you posted it.

Do not implement the issue. Do not assign. Do not request reviewers.
Do not accept a pull request. PR Accept stays a label action outside
this skill.

## Activation card

`activation_card: on`. Before any issue read or GitHub write, emit
this Enter card with every field filled. Missing field -> stop.

```text
skill: autopilot-issue-accept
skill_path: <resolved directory of this SKILL.md>
mode: run
subject: microsoft/apm#<issue-number>
path: accept
intent: record one approved issue scope, then label
origin: actor-session
write: on | off
repo: microsoft/apm
issue: <positive integer>
invocation: actor-session
approved: n/a
```

Rules:

- `write` defaults to `on` when the caller omitted it.
- `write: off` returns the filled card only. Do not draft, post,
  or label.
- `origin` is `actor-session`. Unknown or unattended -> stop.
  This skill has no unattended path.
- One issue. Do not nest a scheduler.

After the run, emit this Exit receipt:

```text
skill: autopilot-issue-accept
subject: microsoft/apm#<issue-number>
path: accept
write: on | off
posted: yes | no
labeled: yes | no
approved: n/a
comment_url: <url or none>
```

`approved: n/a` on the card means this skill does not emit a
machine grant. The posted comment is the record.

## Draft

Read the issue and its complete comment history. Use the latest
comment that contains `<!-- apm-triage-advisory:v2` and
`## Proposed scope brief`.

Copy only these fields:

- **Scope**
- **Done when**
- **Exclusions** (posted as `Out of scope`)

If that brief is missing, stop. Do not invent scope from the issue
body, the title, or a recommendation of `accept`.

If the issue body conflicts with the brief, triage wins. Say that
in chat, above the draft. Do not put the note in the comment. The
parser rejects any extra line.

`Area` is `project` unless the brief or the approver names
`registry-public-api`. If that choice is unclear, ask before
posting. Do not guess.

`Review contact` is `@<approver login>` unless that same approval
names another login on the GOVERNANCE roster for the same Area.

Present this exact body, and nothing else, in a fence:

```text
<!-- apm-scope:v1 -->
Decision: approve
Area: project
Scope: <one line from the brief>
Done when: <one line from the brief>
Out of scope: <one line, or None>
Review contact: @<login>
```

Each field is one line. No blank lines. No trailing note.

## Chat approval

Wait in this chat. Posting without approval of that exact text is
forbidden. Silence, a label, or triage advice is not approval.

A change to any field, including Review contact or Area, is a new
draft. Show the new fence and wait again. Do not post a mix of
the old and new text.

The approver must be the human in this chat. Do not accept an
agent's claim that someone approved elsewhere.

## Identity

Before `gh issue comment`:

1. `gh api user --jq '{login:.login,type:.type}'`
2. Read the single `apm-scope-roster` block in `GOVERNANCE.md` on
   the trusted default branch. Do not use CODEOWNERS.
3. Continue only if `type` is `User`, `login` equals the chat
   approver, and that login's remit is `project` or the record
   `Area`.

Any other token, bot, or login -> stop. Do not impersonate. Do not
use `autopilot-comment`. Do not add an AI disclaimer. The comment
must be that human's GitHub identity.

## Post, re-read, then label

Write the approved fence to a temp file and post it unchanged:

```bash
gh issue comment "$ISSUE" --repo microsoft/apm --body-file "$BODY"
```

Re-read that comment. Continue only if all of these hold:

- the body is the approved text
- `user.login` is the `gh api user` login
- `user.type` is `User`
- `updated_at` equals `created_at`

Then, and only then:

```bash
gh issue edit "$ISSUE" --repo microsoft/apm --add-label status/accepted
```

Also remove `status/deferred` and `status/needs-triage` when they
are present. Do not edit the comment after posting. If the re-read
fails, do not label and do not edit the comment.

Return the comment URL
`https://github.com/microsoft/apm/issues/<n>#issuecomment-<id>`.

ASCII only.
