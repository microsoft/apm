---
name: Autopilot PR Review
description: Queue open pull requests and fan out isolated review slots (default 2)
interval: manual
mode: interactive
input:
  - targets: "PR list (e.g. '#123 #456'), 'queue-open', or a label name"
---

# Autopilot PR Review

Activate **autopilot-pr-review-scheduler**. Targets: **${input:targets}**

1. Build the queue from PR numbers, `queue-open`, or the named label.
2. Fan out through this run's isolated pool (default 2).
3. Never share slots with issue-triage, issue-delivery, or PR-triage.
4. Default `INVOCATION_MODE=session-review`. Use
   `composed-implementation-review` only if the caller asked.
5. Never comment, label, assign, or request reviewers. Slots own
   those writes.
