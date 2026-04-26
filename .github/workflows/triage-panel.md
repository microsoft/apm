---
name: Triage Panel
description: Auto-invoke the apm-triage-panel skill on a daily sweep of untriaged issues plus an opt-in fast path for explicit re-triage. Posts one synthesized verdict per issue and applies the panel-decided labels and milestone, with explicit "agentic proposal pending human ratification" framing.

# Trigger model -- two paths plus manual dispatch:
#
# (1) Daily scheduled sweep (BULK PATH, default for routine intake):
#     Runs once per day, finds open human-authored issues that lack the
#     `status/triaged` label, processes the OLDEST first (so no issue
#     is left behind), capped at MAX_ISSUES_PER_RUN=10. Average issue
#     volume on this repo is ~6.7/day with peaks around 17/day; a
#     10-per-day cap drains queue with margin.
#
# (2) Opt-in re-triage via `status/needs-triage` label (FAST PATH):
#     Maintainers can re-trigger triage on any issue (already-triaged
#     or never-triaged) by applying the `status/needs-triage` label.
#     Fires immediately. Agent runs the panel, refreshes the label set,
#     applies `status/triaged`, and removes `status/needs-triage` so
#     the trigger is consumed. This is the explicit "I need this
#     re-triaged now" signal -- e.g. for security reports or after a
#     major issue body edit.
#
# (3) Manual dispatch with optional `issue_number`: re-runs panel on a
#     specific issue regardless of label state. Useful for replay /
#     debugging.
#
# We deliberately do NOT subscribe to `issues: opened` or `issues:
# reopened` events. Reasons:
#   - Volume: ~200 issues/month at ~50k tokens/run = ~10M tokens/month
#     of LLM cost, with no hard ceiling. Daily-batch path gives a
#     predictable upper bound (10 issues * 30 days = 300 runs/month
#     ceiling) without sacrificing coverage.
#   - Latency: today's manual triage already takes days-to-never; a
#     ~24h agentic latency is a strict improvement and matches OSS
#     issue norms.
#   - Critical-path escape hatch: maintainers who need immediate
#     triage on a specific issue apply `status/needs-triage` (fast
#     path) -- one click, instant.
#
# Front-gates (`on.steps:`) for the labeled fast path:
#   - Triggering label must be `status/needs-triage`. Any other label
#     change is dropped at zero cost (no runner, no agent).
#   - Issue author must not be a Bot.
#   - Issue must be open and unlocked.
on:
  issues:
    types: [labeled]
  schedule:
    # Use gh-aw's fuzzy 'daily' schedule rather than a fixed cron --
    # this distributes execution time across the gh-aw fleet and
    # avoids a deterministic top-of-the-hour load spike.
    - cron: 'daily'
  workflow_dispatch:
    inputs:
      issue_number:
        description: "Optional: specific issue number to triage (overrides sweep). Leave blank to run the daily sweep on demand."
        required: false
        type: string
  steps:
    - name: Front-gate (labeled fast path only)
      id: gate
      if: github.event_name == 'issues'
      env:
        ACTION: ${{ github.event.action }}
        LABEL_NAME: ${{ github.event.label.name }}
        AUTHOR_TYPE: ${{ github.event.issue.user.type }}
        IS_LOCKED: ${{ github.event.issue.locked }}
        STATE: ${{ github.event.issue.state }}
      run: |
        if [ "$ACTION" != "labeled" ]; then
          echo "Action '$ACTION' not subscribed; skipping."
          exit 1
        fi
        if [ "$LABEL_NAME" != "status/needs-triage" ]; then
          echo "Label '$LABEL_NAME' is not 'status/needs-triage'; skipping."
          exit 1
        fi
        if [ "$AUTHOR_TYPE" = "Bot" ]; then
          echo "Issue author is a Bot; skipping."
          exit 1
        fi
        if [ "$IS_LOCKED" = "true" ]; then
          echo "Issue is locked; skipping."
          exit 1
        fi
        if [ "$STATE" != "open" ]; then
          echo "Issue is not open (state=$STATE); skipping."
          exit 1
        fi
        echo "Fast-path gate passed: opt-in re-triage requested by maintainer."
  roles: [admin, maintainer, write]

# Concurrency: never run two triage workflows against the same issue
# simultaneously. For schedule and workflow_dispatch (no specific
# issue), serialize by run kind so two daily sweeps can't overlap.
concurrency:
  group: triage-panel-${{ github.event.issue.number || inputs.issue_number || 'sweep' }}
  cancel-in-progress: false

# Read-only on the agent. Writes (comment, label changes) flow through
# gh-aw safe-outputs (auto-granted scoped write).
permissions:
  contents: read
  issues: read
  # Required by the github MCP toolset 'default' baseline (gh-aw compiler
  # surfaces this even though our prompt only reads issues).
  pull-requests: read

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
#   - add-comment max:12 = up to 10 sweep verdicts + 2 headroom for the
#     fast-path / dispatch single-issue case (which only emits 1).
#   - update-issue lets the agent apply the labels and milestone the
#     panel decides on, plus add `status/triaged` and remove
#     `status/needs-triage` when handling the fast path.
safe-outputs:
  add-comment:
    max: 12
  update-issue:
    target: "*"

timeout-minutes: 30
---

# Triage Panel

You are orchestrating the **apm-triage-panel** skill against issues in
`${{ github.repository }}`. There are three execution modes; pick
exactly one based on the trigger context.

## Mode selection

```
event_name = ${{ github.event_name }}
issue_input = "${{ inputs.issue_number }}"
labelled_issue = "${{ github.event.issue.number }}"
```

- If `event_name == 'issues'` -> **OPT_IN_RETRIAGE** mode on issue
  `${{ github.event.issue.number }}`.
- Else if `event_name == 'workflow_dispatch'` and `inputs.issue_number`
  is non-empty -> **MANUAL_DISPATCH** mode on that single issue.
- Else (`schedule`, or `workflow_dispatch` with no issue number) ->
  **SCHEDULED_SWEEP** mode.

The three modes share Step 2 (run the panel) and Step 3 (apply
decisions). They differ only in Step 1 (which issues to triage) and
post-run housekeeping.

## Universal preconditions

Regardless of mode, before invoking the panel on any single issue you
MUST verify and skip with no comment if:

1. The issue author's `user.type` is `Bot` (e.g. dependabot,
   github-actions, renovate). Bots file structured items that don't
   need agentic triage.
2. The issue is `locked`.
3. The issue's `state` is not `open`.
4. The issue body is empty or only contains the GitHub issue-template
   placeholder text with no real content.

For the SCHEDULED_SWEEP mode these are filters on the candidate list.
For OPT_IN_RETRIAGE the workflow-level front-gate already checks 1-3;
you only need to re-check 4. For MANUAL_DISPATCH check all four; if
any fail, post one short comment explaining why triage was skipped.

## Step 1: Gather candidates

### SCHEDULED_SWEEP

Find up to 10 untriaged open issues, oldest first, excluding bots:

```bash
gh issue list \
  --repo "${{ github.repository }}" \
  --state open \
  --limit 200 \
  --json number,title,author,labels,locked,createdAt,body
```

In your reasoning step (no shell required), filter the result:

- Drop any issue where `author.is_bot` is true or `author.login`
  matches common bot patterns (`*[bot]`, `dependabot*`,
  `github-actions*`, `renovate*`).
- Drop any issue where `locked` is true.
- Drop any issue whose `labels` contains `status/triaged`. That label
  is the explicit "this issue has been through agentic triage" signal.
  A maintainer can re-trigger triage by removing it (sweep re-picks)
  or by applying `status/needs-triage` (fast path).
- Drop any issue with an empty or template-only body.
- Sort the remainder by `createdAt` ascending.
- Take the first **10**.

If after filtering the list is empty, post NO comment. Just exit
cleanly -- a quiet sweep is a healthy sweep.

If more than 10 candidates remain after filtering, that's fine -- the
extras roll to tomorrow's sweep. Do NOT emit a "queued" comment per
rolled issue; just process the 10 you picked.

### OPT_IN_RETRIAGE

The triggering issue is `#${{ github.event.issue.number }}`. Read it:

```bash
gh issue view "${{ github.event.issue.number }}" \
  --repo "${{ github.repository }}" \
  --json number,title,author,labels,locked,state,body,milestone,createdAt
gh issue view "${{ github.event.issue.number }}" \
  --repo "${{ github.repository }}" --comments
```

This is a **re-triage** request from a maintainer. They have already
seen the issue. Treat existing labels (other than `status/needs-triage`
itself, which is the trigger) as **authoritative human-applied state**:
the panel may add new dimensions, but should not silently revert a
maintainer's label choices. If the panel disagrees with an existing
human label, surface it as a brief recommendation in the verdict
comment, do NOT remove the label.

### MANUAL_DISPATCH

The issue is `#${{ inputs.issue_number }}`. Same `gh issue view` calls
as OPT_IN_RETRIAGE. Treat as re-triage if the issue already has any
`theme/*`, `area/*`, or `status/triaged` labels; treat as first-pass
triage otherwise.

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

Run the panel **once per issue**. For SCHEDULED_SWEEP this means up to
10 sequential panel invocations within a single agent run; reset
context between issues so persona reasoning doesn't bleed across
unrelated tickets.

## Step 3: Emit the verdict and apply decisions

For each issue you triaged, emit exactly one comment via
`safe-outputs.add-comment`. The comment body MUST be the skill's
verdict template followed by this footer (verbatim, ASCII only):

```
---

> **Triage status: agentic proposal pending human ratification.**
> Silence is approval. Maintainers can:
> - Override any label or milestone above by editing it directly --
>   human edits are authoritative and will not be reverted on
>   subsequent runs.
> - Re-trigger triage by applying the `status/needs-triage` label, or
>   by removing `status/triaged` to enroll the issue in the next
>   daily sweep.
>
> _Posted by the [Triage Panel workflow](https://github.com/${{ github.repository }}/actions/workflows/triage-panel.lock.yml). See [.apm/skills/apm-triage-panel](https://github.com/microsoft/apm/tree/main/.apm/skills/apm-triage-panel) for the panel skill._
```

Then apply the panel's decided labels + milestone via
`safe-outputs.update-issue`. Required label-set hygiene per issue:

- ADD every `theme/*`, `area/*`, `type/*`, `priority/*` label the
  panel decided on -- but ONLY if the issue does not already have a
  conflicting human-applied label of the same dimension.
- ADD `status/triaged` (mandatory; this is the "do not re-sweep me"
  signal).
- REMOVE `status/needs-triage` if it is currently present (consumes
  the fast-path trigger). Only remove this specific label; never
  remove any other label.
- Apply the panel's recommended milestone if and only if the issue
  has no milestone today. Never overwrite an existing milestone --
  that is a maintainer call.

Do not edit the issue title or body. Do not close, reopen, lock,
unlock, or assign the issue. Do not @-mention any specific contributor
in the verdict comment beyond the issue author (a single courteous
acknowledgement of their report is fine; no maintainer pings).

## Failure handling

If the panel skill fails for a specific issue (e.g., context too
large, ambiguous routing), do NOT post a partial verdict and do NOT
apply any labels for that issue. Skip it silently -- it will be
picked up by the next sweep. The only exception is MANUAL_DISPATCH on
a specific issue: in that case, post a single comment explaining the
failure mode so the dispatcher can iterate.
