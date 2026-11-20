# APM - Agent Package Manager

**An open-source, community-driven dependency manager for the agent supply chain.**

One manifest, one lockfile and one org policy for the skills, plugins and MCP servers your agents load, with the harnesses and marketplaces you already use.

GitHub Copilot | Claude Code | Grok Build | Cursor | OpenCode | Codex | Gemini CLI | Windsurf | Kiro

**[Documentation](https://microsoft.github.io/apm/)** | **[Quickstart](https://microsoft.github.io/apm/quickstart/)** | **[CLI Reference](https://microsoft.github.io/apm/reference/)** | **[Roadmap](https://github.com/orgs/microsoft/projects/2304/views/5)**

---

> **Portable by manifest. Verified by lockfile. Governed by policy.**
> One file declares what your agents load; one lockfile reproduces it on every machine and in CI; one policy decides what your organization allows.

## Why APM

What a coding agent loads decides what it knows and what it can do: skills, instructions, prompts, agents, hooks, plugins and MCP servers. Today teams copy those files between repositories and hand-edit MCP config for every tool. The copies drift, nobody can say which version a repository follows, and nothing reproduces the setup on the next laptop or in CI.

**APM treats them as dependencies.** Declare them once in `apm.yml`. `apm install` writes each one in every harness's own format and records the exact commit, plus a SHA-256 hash of every file it wrote, in `apm.lock.yaml`. Think `package.json` and `package-lock.json`, or `Cargo.toml` and `Cargo.lock`, for your agents, with transitive dependency resolution included.

```yaml
# apm.yml -- ships with your project
name: your-project
version: 1.0.0
targets: [copilot, claude]   # the harnesses this project supports
dependencies:
  apm:
    # A skill, picked from a public collection
    - git: vercel-labs/agent-skills
      skills:
        - deploy-to-vercel
    # A plugin, from Anthropic's marketplace
    - git: https://github.com/anthropics/claude-plugins-official
      path: plugins/commit-commands
      ref: main
  mcp:
    # An MCP server, configured for every harness above
    - name: io.github.github/github-mcp-server
      transport: http   # MCP transport name, not URL scheme -- connects over HTTPS
```

```bash
git clone <org/repo> && cd <repo>
apm install    # the same skills, plugins and MCP servers, from the lockfile
```

**Coming from `npx skills add`?** Drop-in:

```bash
apm install vercel-labs/agent-skills                            # whole bundle, like npx skills add
apm install vercel-labs/agent-skills --skill deploy-to-vercel   # one skill, persisted to apm.yml
```

Same install gesture. You also get a manifest, a lockfile and a fresh clone that matches yours. Coming from Claude Code marketplaces, `gh skill` or hand-copied folders? See [Coming from another tool](https://microsoft.github.io/apm/getting-started/coming-from/).

**Plugins from the marketplaces you already use:**

```bash
apm marketplace add anthropics/claude-code
apm install commit-commands@claude-code-plugins
```

One plugin, written in each harness's own format (commands for Claude Code, prompts for GitHub Copilot) and pinned in the lockfile.

**MCP servers without hand-edited JSON:**

```bash
apm install --mcp io.github.github/github-mcp-server --transport http
```

Every harness keeps MCP servers in its own file: `.mcp.json` for Claude Code and GitHub Copilot, `.cursor/mcp.json` for Cursor, `.codex/config.toml` for Codex. Declare a server once and APM writes each file, on every machine. Add a harness later and the same servers follow. Tokens stay out of the repository as `${VAR}` references, and a server that arrives through someone else's package with its own command is held back until you declare or approve it.

## The three promises

### 1. Portable by manifest

One `apm.yml` declares every primitive your agents need -- instructions, skills, prompts, agents, hooks, plugins, MCP servers -- and `apm install` reproduces the same setup in every harness the project targets, on every machine. No account and no registry: packages come from git.

- **[One manifest, every harness you target](https://microsoft.github.io/apm/reference/targets-matrix/)** -- declared once, written for Copilot, Claude Code, Grok Build, Cursor, OpenCode, Codex, Gemini CLI, Windsurf and Kiro
- **[Skills, one at a time or whole collections](https://microsoft.github.io/apm/consumer/install-packages/)** -- from GitHub, GitHub Enterprise, Azure DevOps, GitLab or a plain git URL
- **[Plugins](https://microsoft.github.io/apm/consumer/install-plugins/)** and **[marketplaces](https://microsoft.github.io/apm/consumer/installing-from-marketplaces/)** -- install Claude Code, GitHub Copilot and Codex plugins by name, locked like any other dependency
- **[MCP servers](https://microsoft.github.io/apm/consumer/install-mcp-servers/)** -- from the GitHub MCP Registry, your own registry, a local command or a remote URL
- **[Transitive dependencies](https://microsoft.github.io/apm/consumer/install-packages/#transitive-dependencies-and-the-lockfile)** -- packages can depend on packages; APM resolves the full tree
- **[Publish packages](https://microsoft.github.io/apm/producer/)** -- tag a release and consumers pin `your-org/your-package#v1.2.0`; fixes reach them as Dependabot or Renovate pull requests
- **[Build plugins](https://microsoft.github.io/apm/producer/build-a-plugin/)** -- keep your plugin's own dependencies declared and locked; `apm pack` writes the `plugin.json` each tool expects, including Codex's `.codex-plugin/plugin.json`
- **[CI/CD ready](https://microsoft.github.io/apm/consumer/run-in-ci/)** -- the [GitHub Action](https://github.com/microsoft/apm-action) installs from the lockfile on every run

### 2. Verified by lockfile

Agent context is executable in effect -- a prompt is a program for an LLM, and hooks and MCP servers start processes on your machine. APM records exactly what you installed and checks it on every clone and every pull request. A lockfile records what arrived, not that it can be trusted: review a new dependency, or a version bump, the way you review code.

- **[Lockfile integrity](https://microsoft.github.io/apm/reference/lockfile-spec/)** -- `apm.lock.yaml` pins each source to a commit and records a SHA-256 hash per package and per deployed file; a download that does not match is refused
- **[Reproducible installs](https://microsoft.github.io/apm/consumer/run-in-ci/)** -- `apm install --frozen` installs exactly what the lockfile records and fails when `apm.yml` disagrees
- **[Drift detection](https://microsoft.github.io/apm/enterprise/drift-detection/)** -- `apm audit` rebuilds what the lockfile describes and diffs it against your working tree; `apm audit --ci` makes it a pull-request check
- **[Content scanning](https://microsoft.github.io/apm/enterprise/security/)** -- every install scans for hidden Unicode that can carry instructions a reviewer cannot see, and blocks critical findings; it looks for hidden characters, not intent
- **[Approvals for hooks and MCP servers](https://microsoft.github.io/apm/consumer/approve-executables/)** -- `apm approve` records, in `apm.yml`, which packages may deploy executables, so the grant is reviewed like any other change
- **[Inventory export](https://microsoft.github.io/apm/reference/cli/lock/)** -- `apm lock export --format cyclonedx|spdx` lists what reached disk, straight from the lockfile -- an inventory for procurement, not an attestation

### 3. Governed by policy

Teams that run coding agents at scale end up building the same thing: a central team publishes approved skills, plugins and MCP servers through its own marketplace, and many repositories install from it. `apm-policy.yml` lets that team say which sources, MCP servers and executables the organization allows, inherited from an enterprise baseline that each organization can tighten but never loosen, and a required `apm audit --ci` check enforces it on every pull request. Policy starts in warn mode; you switch it to block when you are ready. That is the dependency layer for an agentic SDLC.

APM governs what gets installed; your harness governs what runs. Policy is not a sandbox.

- **[Build your organization's marketplace](https://microsoft.github.io/apm/enterprise/org-marketplace/)** -- publish approved capabilities as versioned packages that every repository installs by name and updates on purpose
- **[Set an org policy](https://microsoft.github.io/apm/enterprise/apm-policy/)** -- allow or deny sources, MCP servers and executables, in warn or block mode
- **[Gate pull requests](https://microsoft.github.io/apm/enterprise/enforce-in-ci/)** -- wire `apm audit --ci` into a required check, so a denied source fails the build
- **[Security model](https://microsoft.github.io/apm/enterprise/security/)** -- every control, its default, how to tighten it, and what stays your responsibility
- **[Roll out in stages](https://microsoft.github.io/apm/enterprise/)** -- from lockfiles to a blocking policy across hundreds of repositories, each stage reversible

## Get Started

#### macOS with Homebrew

```bash
brew install apm
```

No custom tap is required. Homebrew manages installation and updates:
use `brew upgrade apm`, not `apm self-update`.

#### Linux / macOS without Homebrew

```bash
curl -sSL https://aka.ms/apm-unix | sh
```

#### Windows

```powershell
irm https://aka.ms/apm-windows | iex
```

Native release binaries are published for macOS, Linux, and Windows x86_64.
For standalone installations, `apm self-update` reuses the matching platform installer.
You do not need Homebrew to use APM.

<details>
<summary>Other install methods: pip, WinGet, and Scoop</summary>

#### Linux / macOS

```bash
# pip (Python 3.11+)
pip install apm-cli
```

#### Windows

```powershell
# WinGet
winget install --id Microsoft.APM --exact --source winget
```

```powershell
# Scoop
scoop install apm
# pip
pip install apm-cli
```

</details>

Already using the Microsoft Homebrew tap? See the
[migration guide](https://microsoft.github.io/apm/getting-started/installation/#migrate-from-the-microsoft-tap).

Then follow the **[Quickstart](https://microsoft.github.io/apm/quickstart/)**: add a skill, a plugin and an MCP server, commit `apm.yml` and `apm.lock.yaml`, and check a teammate's fresh clone against the lockfile, in five minutes.

Teach your coding agent to use APM with the [official guide](https://github.com/microsoft/apm/tree/main/packages/apm-guide) (an expert agent plus the `apm-usage` skill):

```bash
apm install microsoft/apm/packages/apm-guide#v0.37.0
```

## Community

Created by [@danielmeppiel](https://github.com/danielmeppiel). Maintained by [@danielmeppiel](https://github.com/danielmeppiel) and [@sergio-sisternes-epam](https://github.com/sergio-sisternes-epam).

- [Roadmap](https://github.com/orgs/microsoft/projects/2304/views/5)
- [Discussions](https://github.com/microsoft/apm/discussions)
- [Contributing](CONTRIBUTING.md)
- [Adopters](ADOPTERS.md) -- organizations using APM, listed with their consent; [add yours](https://github.com/microsoft/apm/issues/new?template=adopter.yml)
- [AI Native Development guide](https://danielmeppiel.github.io/awesome-ai-native) -- a practical learning path for AI-native development

**In the ecosystem:** [Thoughtworks Technology Radar Vol. 35](https://www.thoughtworks.com/radar/tools/summary/agent-package-manager) places APM in Trial, describing it as a tool that treats agent context as a supply chain. [Dependabot and Renovate](https://microsoft.github.io/apm/consumer/update-and-refresh/) update APM manifests; [JFrog Artifactory](https://microsoft.github.io/apm/enterprise/org-marketplace/) offers Agent Packages repositories. The formats are an open specification, [OpenAPM v0.1](https://microsoft.github.io/apm/specs/openapm-v01/), with conformance tests.

---

**Built on open standards:** [AGENTS.md](https://agents.md) | [Agent Skills](https://agentskills.io) | [MCP](https://modelcontextprotocol.io)

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft trademarks or logos is subject to and must follow [Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general). Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship. Any use of third-party trademarks or logos are subject to those third-party's policies.
