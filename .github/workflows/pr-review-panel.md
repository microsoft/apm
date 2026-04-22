---
name: PR Review Panel
description: Multi-persona expert panel review of labelled PRs, posting a single synthesized verdict comment.

# Triggers (cost-gated, fork-safe, GHES-compatible):
#
# 1. pull_request: only when a maintainer applies the `panel-review` label.
#    We deliberately do NOT subscribe to `synchronize` -- previous behaviour
#    re-ran the panel on every push to a labelled PR, which is wasteful and
#    indistinguishable from a DoS on agent quota. Re-apply the label
#    (remove + add) to re-run after addressing findings.
#
# 2. workflow_dispatch: manual trigger taking a PR number. This is the
#    only path that works for fork PRs on GitHub.com and GHES, because
#    `pull_request` from forks does NOT pass repository secrets
#    (COPILOT_GITHUB_TOKEN etc.), and gh-aw blocks `pull_request_target`
#    on public repos. workflow_dispatch always runs in the base/trusted
#    context with full secrets, regardless of where the PR head lives.
#
# `forks: ["*"]` is retained so the label path also works against fork
# PRs in private/enterprise repos where secrets DO pass to fork PRs
# (per-org setting). On microsoft/apm public, the dispatch path is the
# reliable fork route.
on:
  pull_request:
    types: [labeled]
    names: [panel-review]
    forks: ["*"]
  workflow_dispatch:
    inputs:
      pr_number:
        description: "Pull request number to review (works for fork PRs)"
        required: true
        type: string

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
**#${{ github.event.pull_request.number || inputs.pr_number }}** in `${{ github.repository }}`.

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
PR=${{ github.event.pull_request.number || inputs.pr_number }}
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
