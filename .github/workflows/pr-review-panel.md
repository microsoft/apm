---
name: PR Review Panel
description: Multi-persona expert panel review of labelled PRs, posting a single synthesized verdict comment.

# Trigger: pull_request (NOT pull_request_target -- gh-aw blocks the latter on
# public repos). Cost-gate: only runs when a maintainer applies `panel-review`.
# `synchronize` re-runs on new pushes once the PR carries the label.
# Default fork policy (no `forks:` field) restricts to same-repo PRs only.
on:
  pull_request:
    types: [labeled, synchronize]
    names: [panel-review]

# Agent job runs READ-ONLY. Safe-output jobs are auto-granted scoped write.
permissions:
  contents: read
  pull-requests: read
  issues: read

# Pull panel skill + persona agents from microsoft/apm@main.
# Why main and not ${{ github.sha }}: a malicious PR could otherwise modify
# the panel skill or persona definitions and trick its own review into
# APPROVE. Pinning to main means the review always runs against the
# trusted, already-reviewed panel -- changes to .apm/ only take effect
# after they themselves have been reviewed and merged.
# Same rationale as GitHub Actions' guidance to pin `uses:` to a ref,
# never to the PR's own head.
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

safe-outputs:
  add-comment:
    max: 1

timeout-minutes: 30
---

# PR Review Panel

You are orchestrating the **apm-review-panel** skill against pull request
**#${{ github.event.pull_request.number }}** in `${{ github.repository }}`.

## Step 1: Load the panel skill

The APM bundle has been unpacked into the runner workspace by the `apm` pre-job.
Read the skill definition before doing anything else:

```bash
# The Copilot engine looks for skills under .github/skills/. Confirm and read:
ls .github/skills/apm-review-panel/ 2>/dev/null || ls .apm/skills/apm-review-panel/
cat .github/skills/apm-review-panel/SKILL.md 2>/dev/null \
  || cat .apm/skills/apm-review-panel/SKILL.md
```

The skill describes the seven personas (Python Architect, CLI Logging Expert,
DevX UX Expert, Supply Chain Security Expert, APM CEO, OSS Growth Hacker,
Auth Expert) and the routing rules between them. Each persona is a separate
agent definition under `.github/agents/` (or `.apm/agents/`).

## Step 2: Gather PR context (read-only)

Use `gh` CLI -- never `git checkout` of PR head. We are running in the base
repo context with read-only permissions; the PR diff is the only untrusted
input we touch, and `gh` returns it as inert data.

```bash
PR=${{ github.event.pull_request.number }}
gh pr view "$PR" --json title,body,author,additions,deletions,changedFiles,files,labels
gh pr diff "$PR"
```

## Step 3: Run the panel

Follow the apm-review-panel SKILL.md routing exactly:
- Specialists raise findings against their domain.
- The CEO arbitrates disagreements and makes the strategic call.
- The OSS Growth Hacker side-channels conversion / `WIP/growth-strategy.md`
  insights to the CEO.

Do not skip personas. Do not invent personas not declared in the skill.

## Step 4: Synthesize a single verdict

Compose ONE comment with this structure:

```
## APM Review Panel Verdict

**Disposition**: APPROVE | REQUEST_CHANGES | NEEDS_DISCUSSION

### Per-persona findings
- **Python Architect**: ...
- **CLI Logging Expert**: ...
- **DevX UX Expert**: ...
- **Supply Chain Security Expert**: ...
- **Auth Expert**: ...
- **OSS Growth Hacker**: ...

### CEO arbitration
<one-paragraph synthesis from apm-ceo>

### Required actions before merge
1. ...
2. ...

### Optional follow-ups
- ...
```

Keep total length under ~600 lines. ASCII only -- no emojis, no Unicode
box-drawing (project encoding rule).

## Step 5: Emit the safe output

Post the verdict by writing the comment body to the agent output channel.
The `safe-outputs.add-comment` job will pick it up and post it to PR #$PR.

You do NOT call the GitHub API directly -- write the structured request to
the safe-outputs channel and gh-aw's permission-isolated downstream job
publishes the comment.
