---
name: Triage Panel
description: Auto-invoke the apm-triage-panel skill on new or reopened issues, posting one synthesized triage comment and applying the labels and milestone the panel decides.

# Triggers (cost-gated, fork-safe, GHES-compatible):
#
# 1. issues: opened / reopened / labeled. We listen on plain `issues`
#    (not `issues_target`) because issues -- unlike PRs -- don't have a
#    fork-head untrusted code surface; the only untrusted input is the
#    issue body, which `gh issue view` returns as inert text and the
#    agent reasons over read-only.
#
#    For the `labeled` event specifically, we want to fire ONLY when
#    the triggering label is `status/needs-triage` (matches the skill's
#    declared scope). gh-aw does not expose a `names:` filter on
#    `issues.types`, so the label-name guard is enforced via
#    `on.steps:` -- a pre-activation step that exits non-zero for any
#    label other than `status/needs-triage` on `labeled` events. This
#    kills the entire downstream pipeline (activation, apm bundle
#    restore, agent container) at the cheapest possible point. Same
#    pattern used by `pr-review-panel.md` for the `panel-review` label.
#
#    `opened` and `reopened` always pass the gate -- they unconditionally
#    warrant a triage pass.
#
# 2. workflow_dispatch: manual fallback. Accepts an issue_number for
#    re-triage without label churn. Useful when the panel needs to be
#    re-run after issue body edits or upstream skill revisions.
on:
  issues:
    types: [opened, reopened, labeled]
  workflow_dispatch:
    inputs:
      issue_number:
        description: "Issue number to triage"
        required: true
        type: string
  steps:
    - name: Filter labeled events to status/needs-triage
      id: label_check
      env:
        EVENT_NAME: ${{ github.event_name }}
        ACTION: ${{ github.event.action }}
        LABEL_NAME: ${{ github.event.label.name }}
      run: |
        if [ "$EVENT_NAME" = "workflow_dispatch" ]; then
          echo "Manual workflow_dispatch -- proceeding."
          exit 0
        fi
        if [ "$ACTION" = "opened" ] || [ "$ACTION" = "reopened" ]; then
          echo "Issue $ACTION -- proceeding."
          exit 0
        fi
        if [ "$ACTION" = "labeled" ] && [ "$LABEL_NAME" = "status/needs-triage" ]; then
          echo "Triggering label is 'status/needs-triage' -- proceeding."
          exit 0
        fi
        echo "Event '$ACTION' / label '$LABEL_NAME' is out of scope; skipping."
        exit 1
  roles: [admin, maintainer, write]

# Agent job runs READ-ONLY against the issue. Label / milestone writes
# happen via gh-aw safe-outputs (auto-granted scoped write).
permissions:
  contents: read
  issues: read
  # Required by the github MCP toolset 'default' baseline (gh-aw compiler
  # surfaces this even though our prompt only reads issues). Stays read-only.
  pull-requests: read

# Pull triage skill + persona agents from microsoft/apm@main.
# Why main and not ${{ github.sha }}: keeps the triage panel pinned to
# the trusted, already-reviewed skill -- changes to .apm/ only take
# effect after they themselves have been reviewed and merged. Same
# trust rationale as `pr-review-panel.md`.
imports:
  - uses: shared/apm.md
    with:
      packages:
        - microsoft/apm#main

tools:
  github:
    toolsets: [default]
  bash: true

network:
  allowed:
    - defaults
    - github

# safe-outputs:
#   - add-comment: one synthesized verdict per run (max:2 leaves headroom
#     for a follow-up clarifying comment if the skill emits one; the
#     skill's contract is one comment).
#   - update-issue: lets the agent apply the labels and milestone it
#     decides on. target:"*" allows updating the issue under triage.
safe-outputs:
  add-comment:
    max: 2
  update-issue:
    target: "*"

timeout-minutes: 20
---

# Triage Panel

You are orchestrating the **apm-triage-panel** skill against issue
**#${{ github.event.issue.number || inputs.issue_number }}** in `${{ github.repository }}`.

> The trigger guard runs at the workflow level (`on.steps:`
> pre-activation step `label_check`). If you are reading this prompt,
> the event is `opened`, `reopened`, a `labeled` event with the
> `status/needs-triage` label, or a manual `workflow_dispatch` --
> proceed.

## Step 1: Gather issue context (read-only)

Use `gh` CLI -- never modify the issue at this stage. The issue body
is the only untrusted input we touch, and `gh` returns it as inert
data.

```bash
ISSUE=${{ github.event.issue.number || inputs.issue_number }}
gh issue view "$ISSUE" --json number,title,body,author,labels,milestone,createdAt,state
gh issue view "$ISSUE" --comments
```

If the issue is already closed, post a one-line note to
`safe-outputs.add-comment` saying triage is skipped because the issue
is closed, and stop.

## Step 2: Run the panel via the apm-triage-panel skill

Load the **apm-triage-panel** skill from
`.apm/skills/apm-triage-panel/SKILL.md` (made available via the
`shared/apm.md` import) and follow its execution checklist and output
contract exactly. The skill owns:

- The mandatory persona roster (DevX UX Expert, Supply Chain Security
  Expert, APM CEO arbiter)
- The conditional persona routing (OSS Growth Hacker, Python
  Architect, Doc Writer)
- The pre-arbitration completeness gate
- The single-comment verdict template (synthesized triage decision,
  label set, milestone, suggested next action)
- The one-comment emission contract -- write the final synthesized
  comment to `safe-outputs.add-comment`, not via the GitHub API.

## Step 3: Apply the panel's decisions

After the skill produces its verdict:

1. Emit the synthesized comment via `safe-outputs.add-comment` (the
   skill's contract already requires this).
2. Apply the panel-decided label set and milestone via
   `safe-outputs.update-issue`. The `theme/*` label, if assigned,
   will automatically trigger the existing `project-sync.yml`
   workflow to add the issue to the appropriate PGS board column --
   no extra action needed here.

Do not perform any other writes. Do not edit the issue body or title.
Do not close, reopen, lock, or assign the issue.
