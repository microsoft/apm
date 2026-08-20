---
name: python-architecture
description: >
  Activate when creating new modules, refactoring class hierarchies, introducing
  design patterns, or making changes spanning 3+ files in the APM CLI codebase.
---

# Python Architecture Skill

[Python architect persona](../../../.apm/agents/python-architect.agent.md)

## When to activate

- Creating new Python modules or packages under `src/apm_cli/`
- Refactoring class hierarchies or introducing base classes
- Changes that touch 3+ files with shared logic patterns
- Introducing new design patterns (Strategy, Observer, etc.)
- Cross-cutting concerns (logging, auth, error handling)
- Performance-sensitive paths (parallel downloads, large manifests)

## Key rules

- Follow existing patterns (BaseIntegrator, CommandLogger, AuthResolver) before inventing new ones
- Prefer composition over deep inheritance
- Push shared logic into base classes, not duplicated across siblings
- One canonical owner per decision: route through the existing authority (target_catalog, AuthResolver/host_providers, runtime registry, CommandLogger, CompiledOutputWriter, deployment_ledger, install-outcome, neutral hook IR, BaseIntegrator); extend it, never fork it. Full rule: `.apm/instructions/architecture.instructions.md`
- Lock every centralization with a dual guardrail: a regression test plus a static check in `scripts/lint-architecture-boundaries.sh`
