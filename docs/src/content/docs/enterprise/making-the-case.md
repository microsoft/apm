---
title: "Making the Case"
description: "Talking points, objection handling, and resources for advocating APM adoption within your organization."
sidebar:
  order: 8
---

An internal advocacy toolkit for APM. Each section is self-contained and designed to be copied into RFCs, Slack messages, and proposals.

---

## TL;DR for Leadership

- **APM is an open-source dependency manager for AI agent configuration** — like package.json but for AI tools. It declares what your agents need in one manifest and installs it with one command.
- **One manifest, one command, locked versions.** Every developer gets identical agent setup, every CI run is reproducible. No more configuration drift across teams.
- **Zero lock-in.** APM generates native config files (`.github/`, `.claude/`, `AGENTS.md`). Remove APM and everything still works.

---

## Talking Points by Audience

### For Engineering Management

- **Developer productivity.** Eliminate manual setup of AI agent configurations. New developers run `apm install` and get a working environment in seconds instead of following multi-step setup guides.
- **Consistency across teams.** A single shared package ensures every team uses the same coding standards, prompts, and tool configurations. Updates propagate with a version bump, not a Slack message.
- **Audit trail for compliance.** Every change to agent configuration is tracked through `apm.lock.yaml` and git history. You can answer "what changed, when, and why" for any audit.

### For Security and Compliance

- **Lock file integrity.** `apm.lock.yaml` pins exact versions and commit SHAs for every dependency. No silent updates, no supply chain surprises.
- **Dependency provenance.** Every package resolves to a specific git repository and commit. The full dependency tree is inspectable before installation.
- **No code execution, no runtime.** APM is a dev-time tool only. It copies configuration files — it does not execute code, run background processes, or modify your application at runtime.
- **Full audit trail.** All configuration changes are committed to git. Compliance teams can review agent setup changes through standard code review processes.

### For Platform Teams

- **Standardize AI configuration across N repos.** Publish a shared APM package with your organization's coding standards, approved MCP servers, and prompt templates. Every repo that depends on it stays in sync.
- **Enforce standards via CI gates.** `apm install` blocks packages with critical hidden-character findings — no configuration needed. `apm audit --ci` verifies lockfile consistency. Add `--policy org` for [organizational policy enforcement](../governance/#organization-policy-governance).
- **Version-controlled standards updates.** When standards change, update the shared package and bump the version. Teams adopt updates through normal dependency management, not ad-hoc communication.

### For Individual Developers

- **One command instead of N installs.** `apm install` sets up all your AI tools, plugins, MCP servers, and configuration in one step.
- **Reproducible setup.** Clone a repo, run `apm install`, and get the exact same agent environment as every other developer on the team.
- **No more "works on my machine" for AI tools.** Lock files ensure everyone runs the same versions of the same configurations.

---

## Common Objections

### "Don't plugins and marketplace installs already handle this?"

Plugins handle single-tool installation for a single AI platform. APM adds capabilities that plugins do not provide:

- **Cross-tool composition.** One manifest manages configuration for Copilot, Claude, Cursor, OpenCode, and any other agent runtime simultaneously.
- **Consumer-side lock files.** Plugins install the latest version. APM pins exact versions so your team stays synchronized.
- **CI enforcement.** Content scanning is built into `apm install` — no plugin equivalent exists. `apm audit --ci` adds lockfile consistency checks and `--policy org` enforces organizational rules.
- **Multi-source dependency resolution.** APM resolves transitive dependencies across packages from multiple git hosts.
- **Shared organizational packages.** Plugins are published by tool vendors. APM packages are published by your own teams, containing your own standards and configurations.

Plugins and APM are complementary. APM can install and manage plugins alongside other primitives.

### "Is this just another tool to maintain?"

APM is a dev-time tool with zero runtime footprint. The workflow is:

1. Run `apm install`.
2. Get configuration files.
3. Done.

There is no daemon, no background process, no runtime dependency. It is analogous to running `npm install` — you do not "maintain" npm at runtime. APM runs during setup and CI, then gets out of the way.

Installation is a single binary with no system dependencies. Updates are a binary swap. The total operational surface is: one CLI binary, one manifest file, one lock file.

### "What about vendor lock-in?"

APM outputs native configuration formats: `.github/instructions/`, `.github/prompts/`, `.claude/`, `AGENTS.md`. These are standard files that your AI tools read directly.

If you stop using APM, delete `apm.yml` and `apm.lock.yaml`. Your configuration files remain and continue to work. Zero lock-in by design.

### "We only use one AI tool, not multiple."

Multi-tool support is a bonus, not a requirement. APM provides value with a single AI tool through:

- **Lock file reproducibility.** Every developer and CI run uses the same configuration versions.
- **Shared packages.** Publish and reuse configuration across repositories.
- **CI governance.** Enforce configuration standards automatically.
- **Dependency management.** Declare and resolve transitive dependencies between configuration packages.

### "Our setup is simple, we don't need this."

APM is worth adopting when any of the following apply:

- You use more than 3 plugins or MCP servers.
- Your team has more than 5 developers.
- You need reproducible agent configuration in CI.
- You share configuration standards across multiple repositories.
- You need an audit trail for compliance.

Below that threshold, manual setup is fine. APM is designed to help when manual management stops scaling.

### "What if the project gets abandoned?"

APM generates standard files that work independently of APM. If you stop using APM:

- Your `.github/instructions/`, `.github/prompts/`, and other config files remain and continue working.
- Your AI tools read native config formats, not APM-specific formats.
- You lose automated dependency resolution and lock file management, but your existing setup is unaffected.

This is a deliberate design choice. APM adds value on top of native formats rather than replacing them.

---

## Sample RFC Paragraph

The following is ready to copy into an internal proposal or RFC:

> We propose adopting APM (Agent Package Manager) to manage AI agent configuration across our repositories. APM is an open-source, dev-time tool that provides a declarative manifest (`apm.yml`) and lock file (`apm.lock.yaml`) for AI coding agent setup — instructions, prompts, skills, plugins, and MCP servers. It resolves dependencies, generates native configuration files for each AI platform, and produces reproducible installs from locked versions. APM has zero runtime footprint: it runs during setup and CI, outputs standard config files, and introduces no vendor lock-in. Adopting APM will eliminate manual agent setup for new developers, enforce consistent configuration across teams, and provide an auditable record of all agent configuration changes through git history. The tool is MIT-licensed, maintained under the Microsoft GitHub organization, and supports GitHub, GitLab, Bitbucket, and Azure DevOps as package sources.

---

## Quick Comparison

For stakeholders familiar with existing tools, this comparison clarifies where APM fits.

| Capability | Manual Setup | Single-Tool Plugin | APM |
|------------|-------------|-------------------|-----|
| Install AI tool configs | Copy files by hand | Per-tool marketplace | One command, all tools |
| Version pinning | None | Vendor-controlled | Consumer-side lock file |
| Cross-tool support | N separate processes | Single tool only | Unified manifest |
| Dependency resolution | Manual | None | Automatic, transitive |
| CI enforcement | Custom scripts | Not available | Built into `apm install`; `apm audit --ci` for lockfile + policy checks |
| Shared org standards | Wiki pages, copy-paste | Not available | Versioned packages |
| Audit trail | Implicit via git | Varies by vendor | Explicit via `apm.lock.yaml` |
| Lock-in | To manual process | To specific vendor | None (native output files) |

---

## ROI Framework

Use these categories to estimate return on investment for your organization.

### Time Saved

| Factor | Estimate |
|--------|----------|
| Manual setup time per developer | 15-60 minutes per repository |
| Team size | N developers |
| Onboarding frequency | Per new hire, per new repo, per environment rebuild |
| Standards update propagation | Hours per repo, per update cycle |
| **Savings formula** | Setup time x team size x frequency per quarter |

With APM, setup reduces to `apm install` (under 30 seconds). Standards updates reduce to a version bump in `apm.yml` and a single `apm install`.

**Example calculation.** A team of 20 developers, each setting up 2 new repos per quarter, spending 30 minutes on manual agent configuration per repo: 20 hours per quarter in setup time alone. With APM, that drops to under 20 minutes total.

### Risk Reduced

| Risk | APM Mitigation |
|------|----------------|
| Version drift between developers | Lock file pins exact versions and commit SHAs |
| Configuration divergence across repos | Shared packages enforce a single source of truth |
| Compliance audit gaps | Git history provides full change trail for every config change |
| Unreviewed agent configuration changes | CI gates catch drift before merge |
| Supply chain concerns | Dependency provenance traced to specific git commits |

### Consistency Gains

| Scenario | Without APM | With APM |
|----------|-------------|----------|
| Updating a coding standard across 10 repos | 10 manual PRs, hope nothing is missed | 1 package update, 10 version bumps |
| New developer onboarding | Follow a setup doc, troubleshoot differences | `git clone && apm install` |
| CI reproducibility | "Worked locally" debugging | Locked versions, identical environments |
| Adding a new MCP server to all repos | Manual config in each repo, inconsistent rollout | Add to shared package, teams pull on next install |
| Auditing agent configuration | Grep across repos, compare manually | Review `apm.lock.yaml` diffs in git history |

---

## Resources

| Topic | Link |
|-------|------|
| Quick Start | [Installation](../../getting-started/installation/) |
| APM for Teams | [Team workflows and shared packages](../teams/) |
| CI/CD Integration | [Pipeline setup and enforcement](../../integrations/ci-cd/) |
| Adoption Playbook | [Phased rollout guide](../adoption-playbook/) |
| Why APM | [Problem statement and design principles](../../introduction/why-apm/) |
| How It Works | [Architecture and compilation pipeline](../../introduction/how-it-works/) |
| Manifest Schema | [apm.yml reference](../../reference/manifest-schema/) |
| Key Concepts | [Primitives, packages, and compilation](../../introduction/key-concepts/) |
| Org-Wide Packages | [Publishing shared configuration](../../guides/org-packages/) |

---

## Next Steps

1. Review the [Adoption Playbook](../adoption-playbook/) for a phased rollout plan.
2. Start with a single team or repository as a pilot.
3. Publish a shared package with your organization's standards using the [APM for Teams](../teams/) guide.
4. Add APM to CI and measure adoption over 30 days.
