---
title: "Why APM?"
description: "The problem APM solves — why AI agents need a dependency manager."
sidebar:
  order: 2
---

AI coding agents are powerful — but only when they have the right context. Today, setting up that context is entirely manual.

## The Problem

Every AI-assisted project faces the same setup friction:

1. **Manual configuration** — developers copy instruction files, write prompts from scratch, configure MCP servers by hand.
2. **No portability** — when a new developer clones the repo, none of the AI setup comes with it.
3. **No dependency management** — if your coding standards depend on another team's standards, there's no way to declare or resolve that relationship.
4. **Drift** — without a single source of truth, agent configurations diverge across developers and environments.

This is exactly the problem that package managers solved for application code decades ago. `npm`, `pip`, `cargo` — they all provide a manifest, a resolver, and a reproducible install. AI agent configuration deserves the same.

## How APM Solves It

APM introduces `apm.yml` — a declarative manifest for everything your AI agents need:

```yaml
name: my-project
version: 1.0.0
dependencies:
  apm:
    - anthropics/skills/skills/frontend-design
    - microsoft/apm-sample-package
    - github/awesome-copilot/agents/api-architect.agent.md
```

Run `apm install` and APM:

- **Resolves transitive dependencies** — if package A depends on package B, both are installed automatically.
- **Integrates primitives** -- prompts, agents, commands, skills, and hooks are deployed to `.github/`, `.claude/`, `.cursor/`, `.opencode/`, `.codex/`, and `.gemini/` based on which directories exist.
- **Compiles instructions** -- `apm compile` generates instruction roll-ups (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) that each tool reads natively.

## APM vs. Manual Setup

Consider a project that uses 5 agent plugins across GitHub Copilot and Claude:

**Without APM:**

```bash
# Every developer, every clone, every time
git clone my-project && cd my-project
# Read README for AI setup instructions
# Manually install plugin A (Copilot)
# Manually install plugin B (Copilot)
# Manually install plugin C (Claude)
# Manually install plugin D (Claude)
# Manually install plugin E (shared)
# Hope the versions match what the rest of the team is using
# Hope you didn't miss a step
```

**With APM:**

```bash
git clone my-project && cd my-project
apm install
# Done. All 5 plugins resolved and installed.
```

| | Without APM | With APM |
|---|---|---|
| Setup steps | 5+ install commands | 1 command |
| Version consistency | Hope-based | Lock file enforced |
| New contributor onboarding | Read docs, follow steps, debug mismatches | `apm install` |
| CI/CD reproducibility | Fragile or nonexistent | Deterministic via `apm.lock.yaml` |
| Cross-tool coordination | Manual per tool | Unified manifest |

## What APM Manages

APM handles seven types of agent primitives:

| Primitive | Purpose |
|-----------|---------|
| **Instructions** | Coding standards and guardrails |
| **Skills** | Reusable AI capabilities |
| **Prompts** | Slash commands and workflows |
| **Agents** | Specialized personas |
| **Hooks** | Lifecycle event handlers |
| **Plugins** | Pre-packaged agent bundles |
| **MCP Servers** | Tool integrations |

All declared in one manifest. All installed with one command.

## Developer Stories

**Solo / Small Team (2-5 devs)** — "I use Copilot AND Claude. The project needs 5 plugins. Without APM, every new contributor runs 5 install commands and hopes they got the right versions. With APM, they run `apm install`."

**Mid-size Team (10-50 devs)** — "We have org-wide security standards, team-specific plugins, and project-level config. `apm.yml` composes all three layers through dependency resolution. `apm.lock.yaml` ensures every developer and CI runner gets the exact same setup."

**Enterprise (100+ devs)** — "When security asks 'what agent instructions were active when release 4.2.1 shipped?' — `git log apm.lock.yaml` answers that. Every change to agent configuration is versioned, auditable, and reproducible."

## Design Principles

- **Familiar** — APM works like the package managers you already know.
- **Fast** — install and run in seconds.
- **Open** — built on [AGENTS.md](https://agents.md), [Agent Skills](https://agentskills.io), and [MCP](https://modelcontextprotocol.io).
- **Portable** — install from GitHub, GitLab, Bitbucket, Azure DevOps, or any git host.

## When You Might Not Need APM

APM is not the right tool for every situation:

- **You use a single AI tool with 1-2 plugins** — the overhead of a manifest may not be worth it yet.
- **You work solo and don't need reproducible setups** — if no one else needs to replicate your environment, manual setup is fine.
- **Your org doesn't require audit trails for AI agent configuration** — if compliance isn't a concern, the lock file adds little value.

APM shines when complexity grows: multiple tools, team coordination, compliance requirements, or CI/CD integration. Start without it if your setup is simple. Adopt it when manual management becomes a bottleneck.

## FAQ

**"Don't plugins already handle this?"**

Yes, for single-tool installation. APM adds what plugins don't provide: cross-tool install with one command, consumer-side lock files (plugins have none), CI enforcement, and multi-source composition. APM works WITH plugin ecosystems, not against them.

**"Is APM another tool I have to maintain?"**

APM is a dev-time tool. Run `apm install`, get your files, done. There is no runtime process, no background daemon, no maintenance burden. It runs when you ask it to and does nothing otherwise.

**"What if I stop using APM?"**

Delete `apm.yml` and `apm.lock.yaml`. Your `.github/` and `.claude/` config files still work exactly as they did before. APM deploys standard files in standard locations. Zero lock-in by design.
