---
name: Docs Sync Advisory
description: Per-PR documentation impact panel; posts a single advisory recommendation comment.

# Triggers (cost-gated, fork-safe, GHES-compatible):
#
# 1. pull_request_target: fires when a label is applied. We use _target
#    (NOT plain pull_request) so that fork PRs run in the BASE repo
#    context with full secrets (COPILOT_GITHUB_TOKEN etc.). gh-aw does
#    not expose `names:` on `pull_request_target` in v0.68.x (the
#    first-class `on.labels` filter landed post-v0.71.1 and is not yet
#    released, see github/gh-aw ADR-28737). To filter by label name
#    without producing a red-X failed CI check on every unrelated label
#    change, we use the top-level frontmatter `if:` field below. Same
#    pattern as `pr-review-panel.md`.
#
#    Why pull_request_target is safe here despite the well-known
#    "pwn-request" pattern:
#      - permissions are read-only (no write to contents / actions)
#      - we never `actions/checkout` the PR head; only `gh pr view` /
#        `gh pr diff` which return inert text
#      - imports are pinned to microsoft/apm#main (docs-sync skill +
#        sibling skills + persona definitions are trusted, not from
#        the PR head)
#      - write surfaces are tightly scoped:
#          add-comment max 2 (one advisory + one safety overflow)
#          remove-labels allowed [docs-sync] max 1
#            (clear the trigger label so re-applying it re-runs the
#             skill idempotently)
#      - companion-PR creation (Step 7 of the skill) requires a
#        SECOND label `docs-sync-confirm` -- A9 SUPERVISED EXECUTION
#        boundary. The agent suggests; the maintainer ratifies.
#      - `roles: [admin, maintainer, write]` ensures only repo
#        maintainers can trigger -- matches the trust model that
#        applying the `docs-sync` label requires write access.
#
#    `synchronize` is intentionally NOT subscribed at rung 1.
#    Re-apply the label (remove + add) to re-run after addressing
#    findings. Rung 2 (default-on) will subscribe to synchronize;
#    not yet enabled, see .apm/skills/docs-sync/evals/README.md
#    ship gates.
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
        description: "Pull request number to assess (works for fork PRs)"
        required: true
        type: string
  roles: [admin, maintainer, write]

# Label-name gate: skip (not fail) when the triggering label isn't
# `docs-sync`. gh-aw injects this `if:` into both pre_activation and
# activation jobs, producing a gray Skipped status for unrelated label
# changes instead of a red Failed check. workflow_dispatch is always
# allowed through.
if: ${{ github.event_name == 'workflow_dispatch' || github.event.label.name == 'docs-sync' }}

# Defense-in-depth against the "pwn request" attack pattern: never check out
# the PR head. We only consume the diff via `gh pr view` / `gh pr diff` which
# return inert text. Combined with read-only permissions below, this makes
# arbitrary code in the PR head physically unreachable from the workflow.
checkout: false

# Agent job runs READ-ONLY. Safe-output jobs are auto-granted scoped write.
permissions:
  contents: read
  pull-requests: read
  issues: read

# Pull docs-sync skill + sibling skills + persona agents from
# microsoft/apm@main.
# Why main and not ${{ github.sha }}: a malicious PR could otherwise modify
# the skill or persona definitions and trick its own review. Pinning to
# main means the assessment always runs against the trusted, already-
# reviewed skill -- changes to .apm/ only take effect after they
# themselves have been reviewed and merged.
imports:
  - uses: shared/apm.md
    with:
      target: copilot
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
  # Single advisory comment per run. max:2 is a fail-soft ceiling; the
  # one-comment discipline lives inside the docs-sync skill (idempotent
  # edit-in-place using the stable `## Docs sync advisory` header).
  add-comment:
    max: 2
  # Label cleanup. The orchestrator removes `docs-sync` so re-applying
  # the label re-runs the skill idempotently. `docs-sync-confirm` is
  # NOT swept -- it is the maintainer's ratification signal for the
  # optional companion PR (see Step 7 of the skill).
  remove-labels:
    allowed: [docs-sync]
    max: 1

timeout-minutes: 30
---

# Docs Sync Advisory

You are orchestrating the **docs-sync** skill against pull request
**#${{ github.event.pull_request.number || inputs.pr_number }}** in `${{ github.repository }}`.

> The label-name guard runs at the workflow level via the top-level
> frontmatter `if:` field (skips both `pre_activation` and `activation`
> for unrelated labels). If you are reading this prompt, the triggering
> label is `docs-sync` or this is a manual `workflow_dispatch` --
> proceed.

## Step 1: Gather PR context (read-only)

Use `gh` CLI -- never `git checkout` of PR head. We are running in the
base repo context with read-only permissions; the PR diff is the only
untrusted input we touch, and `gh` returns it as inert data.

```bash
PR=${{ github.event.pull_request.number || inputs.pr_number }}
gh pr view "$PR" --json title,body,author,additions,deletions,changedFiles,files,labels
gh pr diff "$PR"
gh pr diff "$PR" --name-only
```

Also check for the `docs-sync-confirm` label on this PR -- it gates
the optional companion-PR step (Step 7 of the skill).

```bash
gh pr view "$PR" --json labels --jq '.labels[].name' | grep -q docs-sync-confirm && echo "CONFIRM_PRESENT=true" || echo "CONFIRM_PRESENT=false"
```

## Step 2: Run the docs-sync skill

Load the **docs-sync** skill and follow its execution checklist
(Steps 1-7) and output contract exactly. The skill owns:

- Classifier dispatch (the cost gate)
- Localizer or architect dispatch on in_place / structural verdicts
- Per-page fan-out panel (doc-writer + python-architect verifier)
- Editorial-owner and growth-hacker single passes
- CDO synthesis with bounded ALIGNMENT LOOP (N <= 3 redrafts)
- Cost ceiling enforcement (15 LLM calls max per run)
- Single-comment emission via `safe-outputs.add-comment` (NOT the
  GitHub API directly)
- Label sweep via `safe-outputs.remove-labels` (drops `docs-sync` so
  re-applying it re-runs the skill)
- Companion-PR creation IF AND ONLY IF the `docs-sync-confirm`
  label is present (the A9 SUPERVISED EXECUTION boundary)

The skill body is at `.apm/skills/docs-sync/SKILL.md` (resolved
from the import above).

The advisory comment uses the stable header `## Docs sync advisory`
for idempotent edit-in-place on re-runs.
