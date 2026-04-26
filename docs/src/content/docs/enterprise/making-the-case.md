---
title: "Making the Case"
description: "Problem-at-scale narrative, talking points, objection handling, sample RFC, and ROI framework for advocating APM adoption within your organization."
sidebar:
  order: 2
---

An internal advocacy toolkit. The lead section frames the problem; the rest is designed to be lifted directly into RFCs, Slack messages, leadership decks, and proposals.

---

## The problem at scale

Consider a mid-to-large engineering organization: 50 repositories, 200 developers, five AI coding tools (Copilot, Claude, Cursor, OpenCode, Gemini).

Without centralized configuration management, a predictable set of problems emerges:

- **Manual configuration per repo.** Each team sets up agent configuration independently. Conventions diverge. Knowledge silos form. The "right" way to configure an agent depends on who you ask.
- **No audit trail.** When security or compliance asks "what agent configuration was active at release 4.2.1?" -- there is no answer. Configuration files were hand-edited, and no one tracked which version of which plugin was in use.
- **Version drift.** Developer A has v1.2 of a rules plugin. Developer B has v1.4. CI has whatever was last committed. Bugs that only reproduce under specific configurations become difficult to trace.
- **Onboarding friction.** A new developer reads the README, runs N install commands, copies configuration from a colleague's machine, and hopes nothing was missed. The gap between "environment works" and "environment matches the team standard" is invisible.
- **Ungoverned dependencies.** No platform-level control over which plugins, prompts, or MCP servers reach developer workstations -- the same problem regulated industries spent a decade solving for application code, now back in a new form.

These are not hypothetical problems. They are the direct consequence of treating AI agent configuration as a manual, per-developer responsibility rather than as a managed dependency.

## How APM solves this

APM applies the same model that package managers brought to application dependencies -- declare, lock, install, audit -- to AI agent configuration.

### Declare

A single `apm.yml` file in the repository root declares all agent configuration dependencies:

```yaml
dependencies:
  apm:
    - anthropics/skills/skills/frontend-design
    - microsoft/apm-sample-package#v1.0.0
    - github/awesome-copilot/plugins/context-engineering
  mcp:
    - io.github.github/github-mcp-server
```

This file is version-controlled, reviewed in pull requests, and readable by anyone on the team.

### Lock

Running `apm install` resolves every dependency and writes `apm.lock.yaml`, which pins the exact commit of every dependency. The lock file is committed to the repository. Two developers running `apm install` from the same lock file get identical configuration. A CI pipeline running `apm install` gets the same result as a developer workstation.

### Install

`apm install` reads the lock file and deploys configuration into the native formats expected by each tool -- `.github/` for Copilot, `.claude/` for Claude, `.cursor/` for Cursor, `.opencode/` for OpenCode, `.gemini/` for Gemini. APM generates static files and then gets out of the way. There is no runtime, no daemon, no background process.

### Audit

Because `apm.lock.yaml` is a committed file, standard git tooling answers governance questions directly:

- **What changed?** `git diff apm.lock.yaml`
- **When did it change?** `git log apm.lock.yaml`
- **What was active at a specific release?** `git show v4.2.1:apm.lock.yaml`
- **Is this environment current?** `apm audit`

For the full forensic and compliance recipes, see the [Lock File Specification](../../reference/lockfile-spec/#9-auditing-patterns).

---

## TL;DR for Leadership

- **APM is an open-source dependency manager for AI agent configuration** -- like `package.json` but for AI tools. It declares what your agents need in one manifest and installs it with one command.
- **One manifest, one command, locked versions.** Every developer gets identical agent setup, every CI run is reproducible. No more configuration drift across teams.
- **Secure by default and governable.** Hidden-Unicode and content scanners run before any package reaches an agent; `apm-policy.yml` lets a platform team allow-list dependencies, restrict deploy targets, and enforce trust rules across every repo. See [Security Model](../security/) and [Governance](../governance-guide/).
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
- **No code execution, no runtime.** APM is a dev-time tool only. It copies configuration files -- it does not execute code, run background processes, or modify your application at runtime.
- **Org-wide policy enforcement.** `apm-policy.yml` allow-lists dependency repos, restricts MCP transports and deploy targets, and is auto-discovered from the org's `.github` repo. See [Governance](../governance-guide/) for the bypass contract and install-gate guarantees.
- **Full audit trail.** All configuration changes are committed to git. Compliance teams can review agent setup changes through standard code review processes.

### For Platform Teams

- **Standardize AI configuration across N repos.** Publish a shared APM package with your organization's coding standards, approved MCP servers, and prompt templates. Every repo that depends on it stays in sync.
- **Enforce standards via CI gates.** `apm install` blocks packages with critical hidden-character findings -- no configuration needed. `apm audit --ci` verifies lockfile consistency. Add `--policy org` for [organizational policy enforcement](../governance-guide/).
- **Version-controlled standards updates.** When standards change, update the shared package and bump the version. Teams adopt updates through normal dependency management, not ad-hoc communication.

### For Individual Developers

- **One command instead of N installs.** `apm install` sets up all your AI tools, plugins, MCP servers, and configuration in one step.
- **Reproducible setup.** Clone a repo, run `apm install`, and get the exact same agent environment as every other developer on the team.
- **No more "works on my machine" for AI tools.** Lock files ensure everyone runs the same versions of the same configurations.

---

## Common Objections

### "Don't plugins and marketplace installs already handle this?"

Plugins handle single-tool installation for a single AI platform. APM adds capabilities that plugins do not provide:

- **Cross-tool composition.** One manifest manages configuration for Copilot, Claude, Cursor, OpenCode, Gemini, and any other agent runtime simultaneously.
- **Consumer-side lock files.** Plugins install the latest version. APM pins exact versions so your team stays synchronized.
- **CI enforcement.** Content scanning is built into `apm install` -- no plugin equivalent exists. `apm audit --ci` adds lockfile consistency checks and `--policy org` enforces organizational rules.
- **Multi-source dependency resolution.** APM resolves transitive dependencies across packages from multiple git hosts.
- **Shared organizational packages.** Plugins are published by tool vendors. APM packages are published by your own teams, containing your own standards and configurations.

Plugins and APM are complementary. APM can install and manage plugins alongside other primitives.

### "Is this just another tool to maintain?"

APM is a dev-time tool with zero runtime footprint. The workflow is:

1. Run `apm install`.
2. Get configuration files.
3. Done.

There is no daemon, no background process, no runtime dependency. It is analogous to running `npm install` -- you do not "maintain" npm at runtime. APM runs during setup and CI, then gets out of the way.

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

Ready to copy into an internal proposal:

> We propose adopting APM (Agent Package Manager) to manage AI agent configuration across our repositories. APM is an open-source, dev-time tool that provides a declarative manifest (`apm.yml`) and lock file (`apm.lock.yaml`) for AI coding agent setup -- instructions, prompts, skills, plugins, and MCP servers. It resolves dependencies, generates native configuration files for each AI platform, and produces reproducible installs from locked versions. APM has zero runtime footprint: it runs during setup and CI, outputs standard config files, and introduces no vendor lock-in. Adopting APM will eliminate manual agent setup for new developers, enforce consistent configuration across teams, and provide an auditable record of all agent configuration changes through git history. Pre-deploy content scanning and an org-wide `apm-policy.yml` give the security and platform teams the controls they need to govern what reaches developer workstations. The tool is MIT-licensed, maintained under the Microsoft GitHub organization, and supports GitHub, GitLab, Bitbucket, and Azure DevOps as package sources.

---

## Quick Comparison

For stakeholders familiar with existing tools:

| Capability | Manual Setup | Single-Tool Plugin | APM |
|------------|-------------|-------------------|-----|
| Install AI tool configs | Copy files by hand | Per-tool marketplace | One command, all tools |
| Version pinning | None | Vendor-controlled | Consumer-side lock file |
| Cross-tool support | N separate processes | Single tool only | Unified manifest |
| Dependency resolution | Manual | None | Automatic, transitive |
| CI enforcement | Custom scripts | Not available | Built into `apm install`; `apm audit --ci` for lockfile + policy checks |
| Org policy enforcement | Wiki pages, hope | Not available | `apm-policy.yml`, allow-lists, install-time gate |
| Shared org standards | Wiki pages, copy-paste | Not available | Versioned packages |
| Audit trail | Implicit via git | Varies by vendor | Explicit via `apm.lock.yaml` |
| Lock-in | To manual process | To specific vendor | None (native output files) |

---

## ROI Framework

### Time Saved

| Factor | Estimate |
|--------|----------|
| Manual setup time per developer | 15-60 minutes per repository |
| Team size | N developers |
| Onboarding frequency | Per new hire, per new repo, per environment rebuild |
| Standards update propagation | Hours per repo, per update cycle |
| **Savings formula** | Setup time x team size x frequency per quarter |

With APM, setup reduces to `apm install` (under 30 seconds). Standards updates reduce to a version bump in `apm.yml` and a single `apm install`.

**Example.** A team of 20 developers, each setting up 2 new repos per quarter, spending 30 minutes on manual agent configuration per repo: 20 hours per quarter in setup time alone. With APM, that drops to under 20 minutes total.

### Risk Reduced

| Risk | APM Mitigation |
|------|----------------|
| Version drift between developers | Lock file pins exact versions and commit SHAs |
| Configuration divergence across repos | Shared packages enforce a single source of truth |
| Compliance audit gaps | Git history provides full change trail for every config change |
| Unreviewed agent configuration changes | CI gates catch drift before merge |
| Supply chain concerns | Dependency provenance traced to specific git commits; pre-deploy content scanners |
| Ungoverned dependency proliferation | `apm-policy.yml` allow-lists what every repo can install |

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
| Adoption Playbook | [Phased rollout guide](../adoption-playbook/) |
| Governance | [Bypass contract and install gate](../governance-guide/) |
| Security Model | [Supply-chain posture](../security/) |
| CI/CD Integration | [Pipeline setup and enforcement](../../integrations/ci-cd/) |
| Why APM | [Problem statement and design principles](../../introduction/why-apm/) |
| How It Works | [Architecture and compilation pipeline](../../introduction/how-it-works/) |
| Manifest Schema | [apm.yml reference](../../reference/manifest-schema/) |
| Org-Wide Packages | [Publishing shared configuration](../../guides/org-packages/) |

---

## Next Steps

1. Review the [Adoption Playbook](../adoption-playbook/) for a phased rollout plan.
2. Read [Governance](../governance-guide/) end-to-end before making `apm audit --ci` a required check.
3. Start with a single team or repository as a pilot.
4. Publish a shared package with your organization's standards using the [Org-Wide Packages guide](../../guides/org-packages/).
5. Add APM to CI and measure adoption over 30 days.
