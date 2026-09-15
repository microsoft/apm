---
name: Autopilot PR Review
description: Queue PRs labelled panel-review and fan out isolated review slots (default 2)
interval: manual
mode: interactive
input:
  - targets: "PR list (e.g. '#123 #456') or 'queue-open' (panel-review only)"
---

# Autopilot PR Review

Activate **autopilot-pr-review-scheduler**. Targets: **${input:targets}**

1. Build the queue from named PR numbers or open PRs labelled
   `panel-review`. `panel-review` is the only request trigger.
   Never list all open PRs.
2. Fan out through this run's isolated pool (default 2).
3. Never share slots with issue-triage, issue-delivery, or PR-triage.
4. Default `INVOCATION_MODE=session-review`. Use
   `composed-implementation-review` only if the caller asked.
5. Never comment, label, assign, or request reviewers. Slots own
   those writes.
