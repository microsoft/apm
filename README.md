# APM – Agent Package Manager

**An open-source, community-driven dependency manager for AI agents.**

Think `package.json`, `requirements.txt`, or `Cargo.toml` — but for AI agent configuration.

GitHub Copilot · Claude Code · Cursor · OpenCode · Codex · Gemini

**[Documentation](https://microsoft.github.io/apm/)** · **[Quick Start](https://microsoft.github.io/apm/getting-started/quick-start/)** · **[CLI Reference](https://microsoft.github.io/apm/reference/cli-commands/)** · **[Roadmap](https://github.com/orgs/microsoft/projects/2304)**

---

> **Portable by manifest. Secure by default. Governed by policy.**
> One file describes every agent's context; one command reproduces it everywhere; one policy controls what an org will allow.

## Why APM

AI coding agents need context to be useful — standards, prompts, skills, plugins — but today every developer sets this up manually. Nothing is portable nor reproducible. There's no manifest for it.

**APM fixes this.** Declare your project's agentic dependencies once in `apm.yml`, and every developer who clones your repo gets a fully configured agent setup in seconds — with transitive dependency resolution, just like npm or pip. It's also the first tool that lets you **author plugins** with a real dependency manager and export standard `plugin.json` packages.

```yaml
# apm.yml — ships with your project
name: your-project
version: 1.0.0
dependencies:
  apm:
    # Skills from any repository
    - anthropics/skills/skills/frontend-design
    # Plugins
    - github/awesome-copilot/plugins/context-engineering
    # Specific agent primitives from any repository
    - github/awesome-copilot/agents/api-architect.agent.md
    # A full APM package with instructions, skills, prompts, hooks...
    - microsoft/apm-sample-package#v1.0.0
  mcp:
    # MCP servers -- installed into every detected client
    - name: io.github.github/github-mcp-server
      transport: http   # MCP transport name, not URL scheme -- connects over HTTPS
```

```bash
git clone <org/repo> && cd <repo>
apm install    # every agent is configured
```

## The three promises

### 1. Portable by manifest

One `apm.yml` describes every primitive your agents need — instructions, skills, prompts, agents, hooks, plugins, MCP servers — and `apm install` reproduces the exact same setup across every client on every machine. `apm.lock.yaml` pins the resolved tree the way `package-lock.json` does for npm.

- **[One manifest for everything](https://microsoft.github.io/apm/reference/primitive-types/)** — declared once, deployed across Copilot, Claude, Cursor, OpenCode, Codex, Gemini
- **[Install from anywhere](https://microsoft.github.io/apm/guides/dependencies/)** — GitHub, GitLab, Bitbucket, Azure DevOps, GitHub Enterprise, any git host
- **[Transitive dependencies](https://microsoft.github.io/apm/guides/dependencies/)** — packages can depend on packages; APM resolves the full tree
- **[Author plugins](https://microsoft.github.io/apm/guides/plugins/)** — build Copilot, Claude, and Cursor plugins with dependency management, then export standard `plugin.json`
- **[Marketplaces](https://microsoft.github.io/apm/guides/marketplaces/)** — install plugins from curated registries in one command, deployed across all targets and locked
- **[Pack & distribute](https://microsoft.github.io/apm/guides/pack-distribute/)** — `apm pack` bundles your configuration as a zipped package or a standalone plugin
- **[CI/CD ready](https://github.com/microsoft/apm-action)** — GitHub Action for automated workflows

### 2. Secure by default

Agent context is executable in effect — a prompt is a program for an LLM. APM treats it that way. Every install scans for hidden Unicode that can hijack agent behavior; the lockfile pins integrity hashes; transitive MCP servers are gated by trust prompts.

- **[Content security](https://microsoft.github.io/apm/enterprise/security/)** — `apm install` blocks compromised packages before agents read them; `apm audit` runs the same checks on demand
- **[Lockfile integrity](https://microsoft.github.io/apm/enterprise/governance/)** — `apm.lock` records resolved sources and content hashes for full provenance
- **[MCP trust boundaries](https://microsoft.github.io/apm/guides/mcp-servers/)** — transitive MCP servers require explicit consent

### 3. Governed by policy

`apm-policy.yml` lets a security team say *"these are the only sources, scopes, and primitives this org will allow"* and have every `apm install` enforce it — with tighten-only inheritance from enterprise to org to repo, a published bypass contract, and audit-mode CI gates.

- **[Governance Guide](https://microsoft.github.io/apm/enterprise/governance-guide/)** — the canonical enterprise reference: enforcement points, bypass contract, air-gapped story, failure semantics, rollout playbook
- **[Policy reference](https://microsoft.github.io/apm/enterprise/policy-reference/)** — every check, every field, every default
- **[Adoption playbook](https://microsoft.github.io/apm/enterprise/adoption-playbook/)** — staged rollout from warn to block across hundreds of repos
- **[GitHub rulesets integration](https://microsoft.github.io/apm/integrations/github-rulesets/)** — wire `apm audit --ci` into branch protection

## Get Started

#### Linux / macOS

```bash
curl -sSL https://aka.ms/apm-unix | sh
```

#### Windows

```powershell
irm https://aka.ms/apm-windows | iex
```

Native release binaries are published for macOS, Linux, and Windows x86_64. `apm update` reuses the matching platform installer.

<details>
<summary>Other install methods</summary>

#### Linux / macOS

```bash
# Homebrew
brew install microsoft/apm/apm
# pip
pip install apm-cli
```

#### Windows

```powershell
# Scoop
scoop bucket add apm https://github.com/microsoft/scoop-apm
scoop install apm
# pip
pip install apm-cli
```

</details>

Then start adding packages:

```bash
apm install microsoft/apm-sample-package#v1.0.0
```

Or install from a marketplace:

```bash
apm marketplace add github/awesome-copilot
apm install azure-cloud-development@awesome-copilot
```

Or add an MCP server (wired into Copilot, Claude, Cursor, Codex, OpenCode, and Gemini):

```bash
apm install --mcp io.github.github/github-mcp-server --transport http   # connects over HTTPS
```

> *Codex CLI currently does not support remote MCP servers; the install will skip Codex with a notice. Omit `--transport http` to use the local Docker variant on Codex (requires `GITHUB_PERSONAL_ACCESS_TOKEN`).*

See the **[Getting Started guide](https://microsoft.github.io/apm/getting-started/quick-start/)** for the full walkthrough.

## Works with agentrc

[agentrc](https://github.com/microsoft/agentrc) analyzes your codebase and generates tailored agent instructions — architecture, conventions, build commands — from real code, not templates.

Use agentrc to author high-quality instructions, then package them with APM to share across your org. The `.instructions.md` format is shared by both tools — no conversion needed when moving instructions into APM packages.

## Community

Created by [@danielmeppiel](https://github.com/danielmeppiel). Maintained by [@danielmeppiel](https://github.com/danielmeppiel) and [@sergio-sisternes-epam](https://github.com/sergio-sisternes-epam).

- [Roadmap & Discussions](https://github.com/microsoft/apm/discussions/116)
- [Contributing](CONTRIBUTING.md)
- [AI Native Development guide](https://danielmeppiel.github.io/awesome-ai-native) — a practical learning path for AI-native development

---

**Built on open standards:** [AGENTS.md](https://agents.md) · [Agent Skills](https://agentskills.io) · [MCP](https://modelcontextprotocol.io)

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft trademarks or logos is subject to and must follow [Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general). Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship. Any use of third-party trademarks or logos are subject to those third-party's policies.
