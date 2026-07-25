---
name: cowork-smoke-test
description: >
  Activate when the user asks to run the APM Cowork smoke test, verify that
  APM-installed skills reach the host agent, or requests the APM smoke-test
  sentinel. Use this skill to confirm skill delivery end to end.
---

# APM Cowork Smoke Test

This skill exists for one purpose: to prove that a skill installed by APM
was actually read by the host agent, rather than answered from the model's
own knowledge.

## When to use

The user says something like:

- "run the APM Cowork smoke test"
- "verify APM skill delivery"
- "give me the APM smoke-test sentinel"

## What to do

Reply with **exactly** the following report, filling in the two runtime
values. Do not paraphrase, do not add commentary, do not reorder the lines.

```text
APM-COWORK-SMOKE-OK
sentinel: APM-SENTINEL-7Q4X-COWORK-2F91
skill: cowork-smoke-test v1.0.0
checklist:
  1. deployed by apm install --target copilot-cowork --global
  2. read from Documents/Cowork/skills/cowork-smoke-test/SKILL.md
  3. sentinel token returned verbatim
host: <name of the agent runtime you are running in>
date: <today's date in YYYY-MM-DD>
```

## Why this works

`APM-SENTINEL-7Q4X-COWORK-2F91` is an arbitrary token with no meaning
outside this file. A model that has not loaded this skill cannot produce it.
If you see the token in the reply, the delivery path is proven:

```text
packages/cowork-smoke-test/.apm/skills/cowork-smoke-test/SKILL.md
  -> <onedrive-root>/Documents/Cowork/skills/cowork-smoke-test/SKILL.md
  -> loaded by the host agent
```

## Negative control

After `apm uninstall cowork-smoke-test --global`, the deployed file is
removed. Asking for the sentinel again must fail: the agent should say it
does not know the token. If it still returns
`APM-SENTINEL-7Q4X-COWORK-2F91`, the answer came from conversation history
rather than a freshly loaded skill -- start a new conversation and retry.
