---
name: apm-issue-autopilot
activation_card: on
description: >-
  Compatibility alias. Sequences autopilot-issue-triage-scheduler,
  then autopilot-issue-delivery-scheduler, then
  autopilot-pr-review-scheduler. Kept so existing prompts and
  installs still resolve. Do not implement from this file.
---

# Compatibility alias

This skill is replaced by three jobs, run in this session in
order. Do not implement from this file. Do not load assets in
this package.

1. **autopilot-issue-triage-scheduler** -- advice only.
2. **Hard stop.** Do not implement issues that only gained
   `triage/recommended` in this run. Labels are not permission.
3. **autopilot-issue-delivery-scheduler** -- only issues that
   already have `status/accepted` or a named bounded accept from
   the human in this session. Workers re-check
   `scripts/governance/eligibility.cjs`. Unattended ORIGIN never
   implements.
4. **autopilot-pr-review-scheduler** with
   `composed-implementation-review` for PRs those workers opened
   or already linked.

Do not call `eligibility.cjs` from this alias. Do not assign.
Do not contradict CODEOWNERS.
