---
name: APM Issue Autopilot
description: Compatibility alias. Use issue-triage or code instead.
interval: manual
mode: interactive
input:
  - targets: "Issue list (e.g. '#123 #456'), 'queue-all', or 'bugs'"
---

# APM Issue Autopilot (alias)

Do not run this alias.

- Triage: **autopilot-issue-triage-scheduler**
- Code: **autopilot-issue-delivery-scheduler**

Targets: **${input:targets}**
