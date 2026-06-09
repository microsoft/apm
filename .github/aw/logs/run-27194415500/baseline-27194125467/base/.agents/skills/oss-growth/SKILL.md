---
name: oss-growth
description: >-
  Activate for OSS adoption work -- README conversion surfaces,
  quickstart, templates, release announcements, contributor funnel,
  story angles -- and any update to the maintained growth strategy at
  WIP/growth-strategy.md.
---

# OSS Growth Skill

[OSS growth hacker persona](../../agents/oss-growth-hacker.agent.md)

## When to activate

- README hero / quickstart / examples sections
- `docs/` content that affects first-run conversion
- `templates/` (starter projects shape the second-use experience)
- Release notes / launch posts / social copy
- Edits to `WIP/growth-strategy.md`
- Issue templates that affect the contributor funnel
- Any reviewed change that the CEO flags as having growth implications

## Key rules

- `WIP/growth-strategy.md` is **gitignored** (the entire `WIP/`
  directory is excluded; it may not exist in every checkout). Treat it
  as the single source of truth for growth tactics when present;
  create it locally on first use. Append-only for dated tactical
  notes; concise top-level summary kept to one screen. Never stage or
  commit anything under `WIP/`.
- Every conversion surface needs a one-line hook, a runnable example,
  and a clear next step.
- Reinforce the "package manager for AI-native development" frame on
  every surface. Cut anything that dilutes it.
- Side-channel only: never block specialist findings; annotate them
  with growth implications and escalate to the CEO.
