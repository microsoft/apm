---
title: "What is APM?"
description: "Agent Package Manager — the open-source dependency manager for AI agent configuration."
sidebar:
  order: 1
---

Software teams solved dependency management for application code decades ago.
`npm`, `pip`, `cargo`, `go mod` — declare what you need, install it reproducibly, lock versions, ship.

AI agent configuration has no equivalent. Until now.

## What is agent package management?

AI coding agents — GitHub Copilot, Claude, Cursor, OpenCode, Codex, Gemini — are only as
good as the context they receive. That context is made up of instructions,
skills, prompts, agent definitions, hooks, plugins, and MCP server
configurations.

Today, teams manage this context by hand:

- Copy instruction files between repos
- Write prompts from scratch for every project
- Configure MCP servers manually on each developer's machine
- Hope everyone's setup matches

This is the same class of problem that `package.json` solved for JavaScript,
`requirements.txt` for Python, and `Cargo.toml` for Rust. Agent configuration
is infrastructure. It deserves a dependency manager.

**Agent package management** is the practice of declaring, resolving, locking,
and distributing AI agent configuration as versioned, composable packages.

APM is the tool that does it.

## The shape of the problem

Consider what happens when a team adopts AI coding agents without a package
manager:

| Without APM | With APM |
|---|---|
| Each dev configures agents manually | `apm install` sets up everything |
| Instructions drift across machines | `apm.lock.yaml` pins exact versions |
| No way to share or reuse prompts | Publish and install from any git host |
| MCP servers configured per-developer | Declared in manifest, installed consistently |
| Onboarding requires tribal knowledge | Clone, `apm install`, done |
| No audit trail for agent config | Lock file tracks every dependency |

The cost compounds with team size. A 5-person team with manual setup has 5
divergent agent configurations. A 50-person team has 50.

## How APM works

APM introduces `apm.yml` — a declarative manifest for AI agent configuration:

```yaml
name: my-project
version: 1.0.0
dependencies:
  apm:
    - microsoft/apm-sample-package
    - anthropics/skills/skills/frontend-design
    - github/awesome-copilot/agents/api-architect.agent.md
```

One command installs everything:

```bash
apm install
```

APM resolves transitive dependencies, places files in the correct directories,
and generates a lock file that pins every version.

## The seven primitives

APM manages seven types of agent configuration. Each is a first-class citizen
in the manifest and dependency tree.

| Primitive | What it does | Example |
|---|---|---|
| **Instructions** | Coding standards and guardrails | "Use type hints in all Python files" |
| **Skills** | Reusable AI capabilities and workflows | Form builder, code reviewer |
| **Prompts** | Slash commands for common tasks | `/security-audit`, `/design-review` |
| **Agents** | Specialized AI personas | Accessibility auditor, API designer |
| **Hooks** | Lifecycle event handlers | Pre-tool validation, post-tool linting |
| **Plugins** | Pre-packaged agent bundles | Context engineering kit, commit helpers |
| **MCP Servers** | External tool integrations | Database access, API connectors |

These primitives map directly to the configuration surfaces of major AI coding
tools. APM does not invent new abstractions — it manages the ones that already
exist.

For detailed definitions, see [Primitive Types](../../reference/primitive-types/).

## The lifecycle

APM follows a five-stage lifecycle that mirrors how teams actually work with
agent configuration:

```
CONSUME --> COMPOSE --> LOCK --> BUILD --> DISTRIBUTE
```

**Consume.** Install packages from any git host. APM resolves the full
dependency tree and places primitives in the correct directories.

```bash
apm install microsoft/apm-sample-package
```

**Compose.** Combine primitives from multiple sources. Your project's
`apm.yml` is the single source of truth for all agent configuration.

```yaml
dependencies:
  apm:
    - org/team-standards        # company-wide instructions
    - org/api-patterns          # API development skills
    - community/security-audit  # open-source prompt
```

**Lock.** `apm.lock.yaml` pins every dependency to an exact commit. Two developers
running `apm install` on the same lock file get identical setups.

**Build.** `apm compile` produces optimized output files for each AI tool —
`AGENTS.md` for Copilot, Cursor, and Codex; `CLAUDE.md` for Claude.
`apm pack` creates self-contained bundles for portable distribution.

```bash
apm compile
apm pack
```

**Distribute.** Any git repository is a valid APM package. Publish by pushing
to a git remote — no registry required. For offline distribution, CI artifact
pipelines, or air-gapped environments, use `apm pack` and `apm unpack` to
create and consume portable bundles without network access.

## Supported tools

APM deploys and compiles agent configuration into the native format of each
supported tool:

| AI Tool | What `apm install` deploys | What `apm compile` adds | Support level |
|---|---|---|---|
| GitHub Copilot | `.github/instructions/`, `.github/prompts/`, agents, hooks, plugins, MCP | `AGENTS.md` (optional) | **Full** |
| Claude | `.claude/` commands, skills, MCP | `CLAUDE.md` | **Full** |
| Cursor | `.cursor/rules/`, `.cursor/agents/`, skills, hooks, MCP | `.cursor/rules/` (also via compile) | **Full** |
| OpenCode | `.opencode/agents/`, `.opencode/commands/`, skills, MCP | Via `AGENTS.md` | **Full** |
| Codex CLI | -- | `AGENTS.md` | Instructions via compile |
| Gemini | `.gemini/commands/`, `.gemini/skills/`, `.gemini/settings.json` (MCP, hooks) | `GEMINI.md` (instructions) | **Full** |

For tools with **Full** support, `apm install` deploys all primitives in their
native format — no additional steps needed. For other tools, `apm compile`
generates their configuration format from your instructions. See the
[Compilation guide](../../guides/compilation/) for details.

The output is native. Each tool reads its own format — APM is transparent to
the AI agent at runtime.

For setup details, see [IDE and Tool Integration](../../integrations/ide-tool-integration/).

## Install from anywhere

APM installs packages from any git host that supports HTTPS or SSH:

```bash
# GitHub
apm install microsoft/apm-sample-package

# GitLab
apm install gitlab.com/org/repo

# Bitbucket
apm install bitbucket.org/org/repo

# Azure DevOps
apm install dev.azure.com/org/project/_git/repo

# GitHub Enterprise
apm install github.example.com/org/repo
```

Packages are git repositories. If you can clone it, APM can install it.

For authentication setup, see [Authentication](../../getting-started/authentication/).

## Positioning: APM and plugin ecosystems

APM is not a plugin system. It does not compete with GitHub Copilot Extensions,
Claude plugins, or Cursor features. Those systems define *what agents can do*.

APM is the **governance, composition, and reproducibility layer** that sits
underneath:

```
+--------------------------------------------------+
|  AI Coding Tools                                  |
|  (Copilot, Claude, Cursor, OpenCode, Codex, Gemini)|
+--------------------------------------------------+
|  Plugin / Extension Systems                       |
|  (tool-specific capabilities)                     |
+--------------------------------------------------+
|  APM                                              |
|  (dependency management, composition, lock files) |
+--------------------------------------------------+
|  Git                                              |
|  (source of truth, distribution)                  |
+--------------------------------------------------+
```

APM manages *which* configuration gets deployed, *how* it composes, and
*whether* everyone on the team has the same setup. The plugin systems handle
the rest.

## Zero lock-in

APM's output is the native configuration format of each tool. If you stop using
APM:

- Your `AGENTS.md` still works with Copilot and Codex
- Your `CLAUDE.md` still works with Claude
- Your `GEMINI.md` still works with Gemini
- Your `.cursor/rules/` still work with Cursor
- Your `.opencode/` files still work with OpenCode
- Your `.github/prompts/` still work with Copilot

APM adds a dependency management layer. It does not add a runtime dependency.
The compiled output is plain files that each tool already understands.

## Key value propositions

**Reproducibility.** `apm.lock.yaml` guarantees identical agent setups across
developers, CI, and environments. No more "works on my machine" for AI
configuration.

**One-command install.** Clone a repo, run `apm install`, and every primitive
is in place. Onboarding goes from hours of setup to seconds.

**Composition.** Combine packages from your organization, the community, and
your own project. APM resolves the full dependency tree.

**Audit and governance.** The lock file is a complete, diffable record of every
agent configuration dependency. Review it in PRs like any other infrastructure
change.

**Multi-tool output.** Write your configuration once. APM compiles it for
every supported AI tool.

## What's next

- [Installation](../../getting-started/installation/) — get APM running in under a minute
- [Why APM?](../why-apm/) — the problem space in detail
- [How It Works](../how-it-works/) — architecture and compilation pipeline
- [Key Concepts](../key-concepts/) — primitives, manifests, and lock files
