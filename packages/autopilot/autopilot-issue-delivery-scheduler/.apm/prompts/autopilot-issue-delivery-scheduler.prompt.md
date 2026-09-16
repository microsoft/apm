---
name: Autopilot Issue Delivery
description: Queue accepted issues and fan out isolated autopilot-issue-delivery-worker slots (default 2)
interval: manual
mode: interactive
input:
  - targets: "Issue list (e.g. '#123 #456'), 'accepted' / empty for all status/accepted, or 'bugs' for type/bug only"
---

# Autopilot Issue Delivery

Activate **autopilot-issue-delivery-scheduler**. Targets: **${input:targets}**

1. If targets is `bugs` or starts with `bugs `, use selector `bugs`.
   Otherwise selector `all` (every `status/accepted` issue, plus
   named bounded accepts).
2. Fan out through this run's isolated pool (default 2).
3. Never share slots with issue-triage, PR-review, or PR-triage.
4. Do not run triage. Do not review PRs.
