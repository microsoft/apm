---
title: What is APM?
description: APM is a dependency manager for AI agent context, with a lockfile, a policy engine, and one install gesture across every major harness.
sidebar:
  order: 1
---

import { Card, CardGrid } from '@astrojs/starlight/components';

APM is a dependency manager for AI agent context -- skills, prompts, instructions, hooks, MCP servers -- with a lockfile, a policy engine, and one install gesture across every major harness.

## The mental model

APM borrows the manifest-plus-lockfile shape from `npm`, `pip`, and `cargo` and applies it to the files that configure AI coding agents. You declare what your agents need in `apm.yml`, run `apm install`, and APM resolves the full dependency tree -- including transitive dependencies -- into a tree of harness-native files.

`apm.yml` is the manifest. It lists agentic dependencies (skills, prompts, agents, plugins, full APM packages) and MCP servers. `apm.lock.yaml` is the lockfile. It pins every resolved package to an exact source ref and content hash, so two developers running `apm install` against the same lockfile get byte-identical context. Source authoring lives in `.apm/` inside your repo.

The compiled output lives in the directories each harness already reads: `.github/` for Copilot, `.claude/` for Claude Code, `.cursor/` for Cursor, `.codex/` and `AGENTS.md` for Codex, `.gemini/` for Gemini, `.opencode/` for OpenCode, `.windsurf/` for Windsurf. APM does not invent a runtime format. It writes the files each tool already understands and stays out of the way at agent runtime.

## What APM manages

These are the primitive types you can declare in `apm.yml` or ship in a package. Every other concept page links here as the source of truth.

| Primitive | What it is |
|---|---|
| Instructions | Repository-scoped guardrails and coding standards the agent reads on every turn. |
| Skills | Reusable, model-invocable capabilities packaged as Agent Skills. |
| Prompts | Slash commands and saved prompts the user invokes by name. |
| Agents | Specialized personas with their own scope, tools, and system prompt. |
| Hooks | Lifecycle handlers that run before or after agent tool calls. |
| Commands | Custom CLI-style commands a harness exposes inside the agent UI. |
| Plugins | Bundles of the primitives above, packaged for one-shot install. |
| MCP servers | External tools the agent connects to via Model Context Protocol. |

For deeper definitions, see [Primitives and targets](../primitives-and-targets/). For the on-disk layout of a package, see [Package anatomy](../package-anatomy/).

## What APM is not

- **Not a runtime.** APM ships context to the harness; the harness runs the agent. `apm install` writes files and exits.
- **Not an LLM gateway.** APM does not route, proxy, or meter model calls. It does not see your prompts at inference time.
- **Not a fine-tuning tool.** APM versions context, not weights.
- **Not a marketplace.** Any git repository is a valid APM package. Marketplaces are an optional discovery surface, not a requirement.

## The three promises

APM commits to three things. Each gets a one-paragraph summary here; the deep dive lives in [The three promises](../the-three-promises/).

### Portable by manifest

One `apm.yml`. Seven harnesses. Reproducible AI agent setup. Every developer who clones the repo runs `apm install` and gets the same skills, prompts, instructions, hooks, and MCP servers wired into Copilot, Claude, Cursor, OpenCode, Codex, Gemini, and Windsurf. The lockfile pins exact versions and content hashes.

### Secure by default

Every `apm install` scans for hidden Unicode before agents read it. Agent context is executable -- a prompt is a program for an LLM. APM treats it that way. Each install scans for invisible Unicode that can hijack agent behavior, pins content hashes in the lockfile, and gates transitive MCP servers behind explicit trust prompts. `apm audit` rebuilds context in scratch and diffs against your working tree.

### Governed by policy

Org policy enforced at install time, before MCP touches disk. `apm-policy.yml` lets a security team allow-list sources, scopes, and primitives. Every `apm install` runs the policy before writing to disk -- including transitive MCP servers shipped by deep dependencies. Tighten-only inheritance flows enterprise -> org -> repo. `apm audit --ci` wires the same checks into branch protection.

## Where to next

<CardGrid>
  <Card title="Consumer" icon="rocket">
    Run someone's package on your harness.
    [Quickstart](/apm/quickstart/)
  </Card>
  <Card title="Producer" icon="puzzle">
    Author and publish primitives others can install.
    [Primitives and targets](../primitives-and-targets/)
  </Card>
  <Card title="Enterprise" icon="approve-check">
    Gate org installs on policy and audit in CI.
    [Governance overview](/apm/enterprise/governance-overview/)
  </Card>
</CardGrid>
