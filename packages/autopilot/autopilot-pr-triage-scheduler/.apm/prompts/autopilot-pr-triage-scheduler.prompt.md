---
name: Autopilot PR Triage
description: Queue open pull requests and fan out isolated PR-triage worker slots (default 2)
interval: manual
mode: interactive
input:
  - targets: "PR list (e.g. '#123 #456') or 'queue-open'"
---

# Autopilot PR Triage

Activate **autopilot-pr-triage-scheduler**. Targets: **${input:targets}**

1. Named PRs = explicit request. `queue-open` = sweep (oldest
   first, max 10, max two per author) using
   `fetch_queue.py --kind pr` then `triage_state.py`. Sweep
   excludes `triage/recommended` and `status/triaged` at fetch.
2. Fan out through this run's isolated pool (default 2).
3. Never share slots with issue-triage, delivery, or PR-review.
4. Never merge. Never assign. Never request reviewers.
