---
name: autopilot-scheduler-issues
activation_card: on
description: >-
  Compatibility alias. Use autopilot-issue-triage-scheduler or
  autopilot-issue-delivery-scheduler instead. Kept so existing
  prompts and installs still resolve. Do not implement from this
  file.
---

# Compatibility alias

This skill is replaced by two entrypoints:

- **autopilot-issue-triage-scheduler** -- advisory queue
- **autopilot-issue-delivery-scheduler** -- implement accepted issues
  (selector `all` or `bugs`)

Do not triage or implement from this alias file.
