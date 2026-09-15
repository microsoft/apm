---
name: batch-bug-shepherd
description: >-
  Compatibility alias. Use autopilot-issue-delivery-scheduler
  with selector `bugs`, then autopilot-pr-review-scheduler.
  Kept so existing prompts and installs still resolve. Do not
  implement from this file.
---

# Compatibility alias

This skill is replaced by two jobs, run in this session in
order. Do not implement from this file. Do not run
issue-triage from this alias.

1. **autopilot-issue-delivery-scheduler** with selector `bugs`.
   Queue is `status/accepted` or a named bounded accept, not
   `type/bug` alone.
2. **autopilot-pr-review-scheduler** with
   `composed-implementation-review` for resulting or in-flight
   PRs.

Workers re-check governance. Unattended ORIGIN never
implements. Do not assign. Do not contradict CODEOWNERS.
