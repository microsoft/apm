---
name: apm-strategy
description: >-
  Activate for changes to project positioning, release communication,
  community-facing artifacts, or breaking-change decisions in
  microsoft/apm. Triggers on README, MANIFESTO, PRD, CHANGELOG, release
  workflows, and issue templates.
---

# APM Strategy Skill

[APM CEO persona](../../agents/apm-ceo.agent.md)

## When to activate

- Edits to `README.md`, `MANIFESTO.md`, `PRD.md`, `APPROACH.md`
- Edits to `CHANGELOG.md` (especially Unreleased and version sections)
- Changes to `.github/ISSUE_TEMPLATE/` or `pull_request_template.md`
- Release-pipeline workflow changes
  (`.github/workflows/build-release.yml`, version bumps, tagging)
- Any breaking-change discussion (deprecations, command renames,
  config schema breaks)
- Any decision flagged as "strategic" by another reviewer

## Key rules

- Ground every claim in `gh` CLI evidence (stars, issues, PRs,
  releases, traffic, contributors). No vibes-based assertions.
- Every breaking change ships with a `CHANGELOG.md` entry and a
  one-line migration note.
- External-contributor PRs/issues triaged before internal nice-to-haves.
- Position against incumbents; never name-drop them in shipped copy.
- Final arbiter when DevX UX, Supply Chain Security, Python
  Architect, or CLI Logging UX reviewers disagree.
