---
name: apm-review-panel
description: >-
  Orchestrate an expert panel of seven agents for multi-disciplinary
  review of non-trivial changes to microsoft/apm: architecture, CLI
  logging, developer-tooling UX, supply-chain security, strategic
  positioning, and OSS growth. Use for PR reviews, design proposals,
  release decisions, and any cross-cutting change.
---

# APM Review Panel -- Expert Review Orchestration

## Agent roster

| Agent | Persona | Activate for |
|-------|---------|--------------|
| [Python Architect](../../agents/python-architect.agent.md) | Architectural Reviewer | Module structure, design patterns, cross-file refactors |
| [CLI Logging Expert](../../agents/cli-logging-expert.agent.md) | Output UX Reviewer | CommandLogger, `_rich_*`, DiagnosticCollector, verbose-mode behavior |
| [DevX UX Expert](../../agents/devx-ux-expert.agent.md) | Package-Manager UX | Command surfaces, flags, help text, install/init/run flows, error wording |
| [Supply Chain Security Expert](../../agents/supply-chain-security-expert.agent.md) | Threat-Model Reviewer | Dependency identity, lockfile integrity, path safety, token scoping |
| [APM CEO](../../agents/apm-ceo.agent.md) | Strategic Owner / Arbiter | Positioning, breaking-change comms, release decisions, final calls on disagreements |
| [OSS Growth Hacker](../../agents/oss-growth-hacker.agent.md) | Adoption Strategist | Conversion surfaces, story angles, `WIP/growth-strategy.md` (gitignored, maintainer-local) |

## Routing topology

```
  python-architect    cli-logging-expert    devx-ux-expert    supply-chain-security-expert
        \_______________________|______________________________/
                                |
                                v                       <----  oss-growth-hacker
                            apm-ceo                           (annotates findings;
                       (final call / arbiter)                  updates growth-strategy)
```

- **Specialists raise findings independently** -- no implicit consensus.
- **CEO arbitrates** when specialists disagree or when a finding has
  strategic implications (positioning, breaking change, naming, scope).
- **Growth Hacker is a side-channel** to the CEO: never blocks a
  specialist finding; annotates it with growth implications and
  escalates to the CEO when relevant.

## Workflow blocks

### Code review (architecture + logging)
1. Python Architect reviews structure / patterns / cross-file impact.
2. CLI Logging Expert reviews any output / logger changes.
3. CEO ratifies if the two disagree on abstraction vs consistency.

### CLI UX review
1. DevX UX Expert reviews command surface, flags, help, error wording.
2. CLI Logging Expert reviews how outputs are emitted (logger methods).
3. Growth Hacker annotates if the change affects first-run conversion.
4. CEO ratifies any naming / positioning calls.

### Security review
1. Supply Chain Security Expert maps the change to the threat model.
2. DevX UX Expert flags any ergonomics regression from the mitigation.
3. CEO arbitrates trade-offs; bias toward security on default behavior.

### Release / comms review
1. CEO grounds the release framing in `gh` CLI stats.
2. Growth Hacker drafts hook + story angle; updates
   `WIP/growth-strategy.md` (gitignored maintainer-local; create if absent).
3. Specialists sanity-check any technical claims in release notes.

### Full panel review (non-trivial change)
1. Each specialist produces independent findings.
2. Growth Hacker annotates findings with growth implications.
3. CEO synthesizes, resolves disagreements, makes the final call.
4. Surface decision and rationale to the author.

## Quality gates

A non-trivial change passes when:

- [ ] Python Architect: structure / patterns OK (or change explicitly
      justified)
- [ ] CLI Logging Expert: output paths route through CommandLogger,
      no direct `_rich_*` in commands
- [ ] DevX UX Expert: command surface familiar to npm/pip/cargo users,
      every error has a next action
- [ ] Supply Chain Security Expert: no new path / auth / integrity
      surface left unguarded; fails closed
- [ ] APM CEO: trade-offs ratified, breaking changes have CHANGELOG +
      migration line
- [ ] OSS Growth Hacker: conversion surfaces unaffected or improved;
      `WIP/growth-strategy.md` updated if relevant (maintainer-local;
      gitignored, never committed)

## Notes

- Each persona file declares its own boundaries and anti-patterns --
  read them before invoking.
- This skill orchestrates only; persona detail lives in the linked
  `.agent.md` files (progressive disclosure).
