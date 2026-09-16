---
name: Autopilot Scheduler Issues
description: Compatibility alias. Use issue-triage or code instead.
interval: manual
mode: interactive
input:
  - targets: "Issue list, 'queue-all', or 'bugs'"
---

# Autopilot Scheduler Issues (alias)

Do not run this alias.

- Triage: activate **autopilot-issue-triage-scheduler**
- Code: activate **autopilot-issue-delivery-scheduler**
  (selector `bugs` if targets starts with `bugs`)

Targets: **${input:targets}**
