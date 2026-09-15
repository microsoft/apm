---
name: shepherd-driver
description: >-
  Compatibility alias. Use autopilot-pr-review-worker instead.
  Kept so existing prompts and installs still resolve. Do not
  implement from this file.
---

# Compatibility alias

This skill is replaced by **autopilot-pr-review-worker**.

A parent scheduler (`autopilot-pr-review-scheduler`)
spawns that worker for ONE open PR when drive-to-merge was asked.
Probe
`../autopilot-pr-review-worker/assets/worker-prompt.md`.

Do not drive PRs from this alias file.
