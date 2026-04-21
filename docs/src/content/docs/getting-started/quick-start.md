---
title: "Quick Start"
description: "Get APM running and install your first package in under 3 minutes."
sidebar:
  order: 2
---

Three commands. Three minutes. Your AI agent learns your project's standards automatically.

## Install APM

**macOS / Linux:**

```bash
curl -sSL https://aka.ms/apm-unix | sh
```

**Windows (PowerShell):**

```powershell
irm https://aka.ms/apm-windows | iex
```

Verify it worked:

```bash
apm --version
```

For Homebrew (macOS/Linux), Scoop (Windows), pip, or manual install, see the [Installation guide](../installation/).

## Start a project

Create a new project:

```bash
apm init my-project && cd my-project
```

Or initialize inside an existing repository:

```bash
cd your-repo
apm init
```

Either way, APM creates an `apm.yml` manifest -- your dependency file for AI agent configuration:

```yaml title="apm.yml"
name: my-project
version: 1.0.0
dependencies:
  apm: []
```

## Install a package

This is where it gets interesting. Install a package and watch what happens:

```bash
apm install microsoft/apm-sample-package#v1.0.0
```

APM downloads the package, resolves its dependencies, and deploys files directly into the directories your AI tools already watch:

```
my-project/
  apm.yml
  apm.lock.yaml
  apm_modules/
    microsoft/
      apm-sample-package/
  .github/
    instructions/
      apm-sample-package/
        design-standards.instructions.md
    prompts/
      apm-sample-package/
        accessibility-audit.prompt.md
        design-review.prompt.md
  .claude/
    commands/
      apm-sample-package/
        ...
  .cursor/
    rules/
      design-standards.mdc
    agents/
      design-reviewer.md
  .opencode/
    agents/
      design-reviewer.md
    commands/
      design-review.md
```

Three things happened:

1. The package was downloaded into `apm_modules/` (like `node_modules/`).
2. Instructions, agents, and skills were deployed to `.github/`, `.claude/`, `.cursor/`, and `.opencode/` (when present) -- the native directories that GitHub Copilot, Claude, Cursor, and OpenCode read from. If the project has its own `.apm/` content, that is deployed too (local content takes priority over dependencies on collision).
3. A lockfile (`apm.lock.yaml`) was created, pinning the exact commit so every team member gets identical configuration.

Your `apm.yml` now tracks the dependency:

```yaml title="apm.yml"
name: my-project
version: 1.0.0
dependencies:
  apm:
    - microsoft/apm-sample-package#v1.0.0
```

## That's it

Open your editor. GitHub Copilot, Claude, Cursor, and OpenCode pick up the new context immediately -- no extra configuration, no compile step, no restart. The agent now knows your project's design standards, can run your prompt templates, and follows the conventions defined in the package.

This is the core idea: **packages define what your AI agent knows, and `apm install` puts that knowledge exactly where your tools expect it.**

## Day-to-day workflow

When a new developer joins your team:

```bash
git clone <your-repo>
cd <your-repo>
apm install
```

The lockfile ensures everyone gets the same agent configuration. Same as `npm install` after cloning a Node project.

Add more packages as your project evolves:

```bash
apm install github/awesome-copilot/skills/review-and-refactor
```

**What to commit:**
- `apm.yml` and `apm.lock.yaml` — version-controlled, shared with the team.
- `.github/` deployed files (`prompts/`, `agents/`, `instructions/`, `skills/`, `hooks/`) — commit them so every contributor (and [Copilot on github.com](https://docs.github.com/en/copilot)) gets agent context immediately after cloning, before they run `apm install` to sync and regenerate files.
- `.claude/` deployed files (`agents/`, `commands/`, `skills/`, `hooks/`) — same rationale for Claude Code users: committed files give instant context on clone, while `apm install` remains the way to refresh them from `apm.yml`.
- `.cursor/` deployed files (`rules/`, `agents/`, `skills/`, `hooks/`) — same rationale for Cursor users.
- `apm_modules/` — add to `.gitignore`. Rebuilt from the lockfile on install.

:::tip[Keeping deployed files in sync]
When you update `apm.yml`, re-run `apm install` and commit the changed `.github/`, `.claude/`, and `.cursor/` files. A [CI drift check](../../integrations/ci-cd/#verify-deployed-primitives) catches stale files automatically.
:::

:::note[Using Codex or Gemini?]
These tools use different configuration formats. Run `apm compile` after installing to generate their native files. See the [Compilation guide](../../guides/compilation/) for details.
:::

## Add MCP servers

APM also manages MCP servers -- the tools your AI agent calls at runtime.

```bash
apm install --mcp io.github.github/github-mcp-server
```

This wires the server into every detected client (Copilot, Claude, Cursor, Codex, OpenCode). See the [MCP Servers guide](../../guides/mcp-servers/) for stdio and remote shapes.

## Next steps

- [Your First Package](../first-package/) -- create and share your own APM package.
- [Dependency management](../../guides/dependencies/) -- version pinning, updates, and transitive resolution.
- [CLI reference](../../reference/cli-commands/) -- full list of commands and options.
