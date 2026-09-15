---
name: shepherd-driver
description: >-
  Compatibility alias. Use autopilot-pr-merge-worker instead.
  Kept so existing prompts and installs still resolve. Do not
  implement from this file.
---

# Compatibility alias

This skill is replaced by **autopilot-pr-merge-worker**.

Invoke that worker by name. `autopilot-pr-review-scheduler`
never composes it.

Do not drive PRs from this alias file.
