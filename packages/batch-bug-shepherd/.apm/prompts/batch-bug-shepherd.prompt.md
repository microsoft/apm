---
name: Batch Bug Shepherd
description: Compatibility alias. Use delivery selector bugs, then PR review.
interval: manual
mode: interactive
input:
  - targets: "Issue list (e.g. '#123 #456'), 'queue-all', or 'bugs'"
---

# Batch Bug Shepherd (alias)

Do not run this alias as an engine.

- Code: **autopilot-issue-delivery-scheduler** with selector `bugs`
- Review: **autopilot-pr-review-scheduler**

Targets: **${input:targets}**
