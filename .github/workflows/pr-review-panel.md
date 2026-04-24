---
name: PR Review Panel
description: Multi-persona expert panel review of labelled PRs, posting a single synthesized verdict comment.

# Triggers (cost-gated, fork-safe, GHES-compatible):
#
# 1. pull_request_target: fires when a label is applied. We use _target
#    (not plain pull_request) so that fork PRs run in the BASE repo
#    context with full secrets (COPILOT_GITHUB_TOKEN etc.). The label
#    name is filtered inside the prompt (Step 0) -- gh-aw does not
#    expose `names:` on pull_request_target.
#
#    Why pull_request_target is safe here despite the well-known
#    "pwn-request" pattern:
#      - permissions are read-only (no write to contents / actions)
#      - we never `actions/checkout` the PR head; only `gh pr view` /
#        `gh pr diff` which return inert text
#      - imports are pinned to microsoft/apm#main (panel skill +
#        persona definitions are trusted, not from the PR)
#      - the only write surface is safe-outputs.add-comment (max 7
#        is a safety ceiling; the agent is instructed to emit one
#        synthesized verdict comment)
#      - `roles: [admin, maintainer, write]` ensures only repo
#        maintainers can trigger -- matches the trust model that
#        applying the `panel-review` label requires write access.
#
#    `synchronize` is intentionally NOT subscribed: previous behaviour
#    re-ran the panel on every push to a labelled PR, which burned
#    agent quota. Re-apply the label (remove + add) to re-run after
#    addressing findings.
#
# 2. workflow_dispatch: manual fallback. Reads YAML from the dispatched
#    ref (default main) and accepts any PR number. Useful if a
#    maintainer needs to re-run without touching labels.
on:
  pull_request_target:
    types: [labeled]
  workflow_dispatch:
    inputs:
      pr_number:
        description: "Pull request number to review (works for fork PRs)"
        required: true
        type: string
  roles: [admin, maintainer, write]

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
    max: 7

timeout-minutes: 30
---

# PR Review Panel

You are orchestrating the **apm-review-panel** skill against pull request
**#${{ github.event.pull_request.number || inputs.pr_number }}** in `${{ github.repository }}`.

## Step 0: Label-name guard (skip when irrelevant)

`pull_request_target: types: [labeled]` fires for ANY label change. Bail
immediately unless the triggering label is `panel-review` (or this is a
manual `workflow_dispatch`):

```bash
EVENT="${{ github.event_name }}"
LABEL="$(jq -r '.label.name // ""' "$GITHUB_EVENT_PATH")"
if [ "$EVENT" = "pull_request_target" ] && [ "$LABEL" != "panel-review" ]; then
  echo "Triggering label is '$LABEL' (not 'panel-review'); exiting cleanly."
  exit 0
fi
```

## Step 1: Gather PR context (read-only)

Use `gh` CLI -- never `git checkout` of PR head. We are running in the base
repo context with read-only permissions; the PR diff is the only untrusted
input we touch, and `gh` returns it as inert data.

```bash
PR=${{ github.event.pull_request.number || inputs.pr_number }}
gh pr view "$PR" --json title,body,author,additions,deletions,changedFiles,files,labels
gh pr diff "$PR"
```

## Step 2: Run the panel via the apm-review-panel skill

Load the **apm-review-panel** skill and follow its execution checklist
and output contract exactly. The skill owns reviewer routing, persona
dispatch, the Auth Expert conditional rule, the pre-arbitration
completeness gate, CEO arbitration, template loading, verdict shape,
and the one-comment emission contract -- including writing the final
comment to `safe-outputs.add-comment` rather than the GitHub API.
