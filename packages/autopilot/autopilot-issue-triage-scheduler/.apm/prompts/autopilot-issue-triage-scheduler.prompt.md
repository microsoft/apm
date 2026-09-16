---
name: Autopilot Scheduler Issue Triage
description: Queue open issues and fan out isolated issue-triage-worker slots (default 2)
interval: manual
mode: interactive
input:
  - targets: "Issue list (e.g. '#123 #456') or 'queue-all'"
---

# Autopilot Scheduler Issue Triage

Activate **autopilot-issue-triage-scheduler**. Targets: **${input:targets}**

1. Named issues = explicit request. `queue-all` = sweep (oldest
   first, max 10, max two per author) using
   `fetch_queue.py` then `triage_state.py`.
2. Fan out through this run's isolated pool (default 2).
3. Never share slots with delivery, PR-review, or PR-triage.
4. Never implement. Never assign.
