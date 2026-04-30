---
name: PR Review Panel
description: Multi-persona expert panel review of labelled PRs, posting a single synthesized verdict comment.

# Triggers (cost-gated, fork-safe, GHES-compatible):
#
# 1. pull_request_target: fires when a label is applied. We use _target
#    (not plain pull_request) so that fork PRs run in the BASE repo
#    context with full secrets (COPILOT_GITHUB_TOKEN etc.). gh-aw does
#    not expose `names:` on `pull_request_target` in v0.68.x (the
#    first-class `on.labels` filter landed post-v0.71.1 and is not yet
#    released, see github/gh-aw ADR-28737). To filter by label name
#    without producing a red-X failed CI check on every unrelated label
#    change, we use the top-level frontmatter `if:` field below: gh-aw
#    propagates that condition to BOTH the `pre_activation` and
#    `activation` jobs, so unmatched labels yield a clean gray Skipped
#    status (no failed run, no quota burn, no agent cold-start).
#    Previously this was implemented as an `on.steps:` step that called
#    `exit 1` to kill the pipeline -- correct gating, but it marked
#    every unrelated `labeled` event as a Failed check, polluting CI
#    dashboards on PRs that touch many labels. Replace with `on.labels:
#    [panel-review]` once gh-aw releases a version that supports it on
#    `pull_request_target`.
#
#    Why pull_request_target is safe here despite the well-known
#    "pwn-request" pattern:
#      - permissions are read-only (no write to contents / actions)
#      - we never `actions/checkout` the PR head; only `gh pr view` /
#        `gh pr diff` which return inert text
#      - imports are pinned to microsoft/apm#main (panel skill +
#        persona definitions are trusted, not from the PR)
#      - write surfaces are tightly scoped:
#          add-comment max 2 (one CEO comment + one safety overflow)
#          add-labels allowed [panel-approved, panel-rejected] max 1
#            (mutually exclusive verdict; orchestrator emits exactly one)
#          remove-labels allowed [panel-review] max 1
#            (clear the trigger label after the run so re-applying it
#             re-runs the panel idempotently)
#        The verdict labels themselves are stripped on every new push
#        by the deterministic companion workflow pr-panel-label-reset.yml
#        (plain GitHub Actions, no LLM).
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

# Label-name gate: skip (not fail) when the triggering label isn't
# `panel-review`. gh-aw injects this `if:` into both pre_activation and
# activation jobs, producing a gray Skipped status for unrelated label
# changes instead of a red Failed check. workflow_dispatch is always
# allowed through. See trigger comment block above for context.
if: ${{ github.event_name == 'workflow_dispatch' || github.event.label.name == 'panel-review' }}

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
  # Single CEO comment per panel run. max:2 is a fail-soft ceiling; the
  # one-comment discipline lives inside the apm-review-panel skill.
  add-comment:
    max: 2
  # Verdict label. Mutually exclusive (orchestrator picks exactly one).
  # The companion workflow pr-panel-label-reset.yml strips both on every
  # new push so a stale verdict can never linger past a code change.
  add-labels:
    allowed: [panel-approved, panel-rejected]
    max: 1
  # Trigger label cleanup. Removed after the run so re-applying
  # `panel-review` re-triggers the panel cleanly.
  remove-labels:
    allowed: [panel-review]
    max: 1

timeout-minutes: 30
---

# PR Review Panel

You are orchestrating the **apm-review-panel** skill against pull request
**#${{ github.event.pull_request.number || inputs.pr_number }}** in `${{ github.repository }}`.

> The label-name guard runs at the workflow level via the top-level
> frontmatter `if:` field (skips both `pre_activation` and `activation`
> for unrelated labels). If you are reading this prompt, the triggering
> label is `panel-review` or this is a manual `workflow_dispatch` --
> proceed.

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
