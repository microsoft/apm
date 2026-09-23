---
name: Autopilot PR Review
description: Queue PRs labelled panel-review or status/accepted and fan out isolated review slots (default 2)
interval: manual
mode: interactive
input:
  - targets: "PR list (e.g. '#123 #456') or 'queue-open' (panel-review union status/accepted)"
---

# Autopilot PR Review

Activate **autopilot-pr-review-scheduler**. Targets: **${input:targets}**

1. Build the queue from named PR numbers or the unsteered sweep:
   open PRs labelled `panel-review` union open PRs labelled
   `status/accepted` on the PR. `panel-review` is the only
   request trigger. `status/accepted` on the PR is a sweep
   source. Never list all open PRs.
2. Fan out through this run's isolated pool (default 2).
3. Never share slots with issue-triage, issue-delivery, or PR-triage.
4. `INVOCATION_MODE=session-review` only. Never compose
   `autopilot-pr-merge-worker`. Never implement.
5. Never comment, label, assign, or request reviewers. Slots own
   those writes.
