---
title: "Agent Workflows (Experimental)"
description: "Run agentic workflows locally using APM scripts and AI runtimes."
sidebar:
  order: 9
---

:::caution[Experimental Feature]
APM's core value is dependency management — `apm install`, `apm.lock.yaml`, `apm audit`. The workflow execution features described on this page are experimental and may change. For most users, `apm install` is all you need.
:::

## What are Agent Workflows?

Agent workflows let you run `.prompt.md` files locally through AI runtimes — similar to [GitHub Agentic Workflows](https://github.blog/changelog/2025-05-19-github-copilot-coding-agent-in-public-preview/), but on your machine.

Scripts are defined in `apm.yml` or auto-discovered from installed packages. You execute them with `apm run`, passing parameters and choosing a runtime (Copilot CLI, Codex, LLM).

## Setting Up a Runtime

Before running workflows, install at least one AI runtime:

```bash
# GitHub Copilot CLI (recommended)
apm runtime setup copilot

# OpenAI Codex CLI
apm runtime setup codex

# LLM library
apm runtime setup llm
```

Verify installed runtimes:

```bash
apm runtime list
```

### Runtime requirements

| Runtime | Requirements | Notes |
|---------|-------------|-------|
| Copilot CLI | Node.js v22+, npm v10+ | Recommended. MCP config at `~/.copilot/` |
| Codex | Node.js | Set `GITHUB_TOKEN` for GitHub Models support |
| LLM | Python 3.10+ | Supports multiple model providers |

**Copilot CLI** is the recommended runtime — it requires no API keys for installation and integrates with GitHub Copilot directly.

For **Codex**, configure authentication after setup:

```bash
export GITHUB_TOKEN=your_github_token
```

For **LLM**, configure at least one model provider:

```bash
llm keys set github      # GitHub Models (free)
llm keys set openai      # OpenAI
llm keys set anthropic   # Anthropic
```

For more details on runtime capabilities and configuration, see the [Runtime Compatibility](../../integrations/runtime-compatibility/) page.

## Defining Scripts

### Explicit scripts in apm.yml

Define scripts in your `apm.yml` to map names to prompt files and runtimes:

```yaml
scripts:
  start:
    description: "Default workflow"
    prompt: .apm/prompts/start.prompt.md
    runtime: copilot
  review:
    description: "Code review"
    prompt: .apm/prompts/review.prompt.md
    runtime: copilot
  analyze:
    description: "Log analysis"
    prompt: .apm/prompts/analyze-logs.prompt.md
    runtime: llm
```

You can also use the shorthand format for simple scripts:

```yaml
scripts:
  start: "copilot --full-auto -p analyze-logs.prompt.md"
  debug: "RUST_LOG=debug codex analyze-logs.prompt.md"
  llm-script: "llm analyze-logs.prompt.md -m github/gpt-4o-mini"
```

### Auto-discovery (zero configuration)

When you install packages that include `.prompt.md` files, APM auto-discovers them as runnable scripts — no `apm.yml` configuration needed:

```bash
apm install github/awesome-copilot/skills/review-and-refactor
apm run review-and-refactor    # Works immediately
```

APM searches for prompts in this order:

1. Local prompts in the project
2. `.apm/prompts/` directory
3. `.github/prompts/` directory
4. Installed package dependencies

Use `apm list` to see all available scripts (both configured and auto-discovered).

### Handling name collisions

If multiple packages provide prompts with the same name, use qualified paths:

```bash
apm run github/awesome-copilot/code-review --param pr_url=...
apm run acme/standards/code-review --param pr_url=...
```

## Running Workflows

### Basic execution

```bash
apm run start
```

### Passing parameters

Use `--param` to pass input values that map to `${input:name}` placeholders in prompt files:

```bash
apm run start --param service_name=api-gateway --param time_window="1h"
apm run code-review --param pull_request_url="https://github.com/org/repo/pull/123"
```

### Previewing before running

Preview the compiled prompt (with parameters substituted) without executing it:

```bash
apm preview start --param service_name=api-gateway --param time_window="1h"
```

### Listing available scripts

```bash
apm list
```

This shows all scripts — both explicitly defined in `apm.yml` and auto-discovered from installed packages.

## Prompt File Structure

Prompt files (`.prompt.md`) use YAML frontmatter for metadata and Markdown for the prompt body:

```markdown
---
description: Analyzes application logs to identify errors and patterns
author: DevOps Team
mcp:
  - logs-analyzer
input:
  - service_name
  - time_window
  - log_level
---

# Analyze Application Logs

You are an expert DevOps engineer specializing in log analysis.

## Context

- Service: ${input:service_name}
- Time window: ${input:time_window}
- Log level: ${input:log_level}

## Task

1. Retrieve logs for the specified service
2. Identify error patterns and anomalies
3. Suggest remediation steps
```

Use `${input:parameter_name}` syntax for dynamic values that are filled in at runtime via `--param`.

For full details on prompt file syntax, compilation, and dependency management, see the [Prompts guide](../prompts/).

## Example Workflows

### Code review

Install a code review prompt and run it against a pull request:

```bash
apm install github/awesome-copilot/skills/review-and-refactor

apm run review-and-refactor \
  --param pull_request_url="https://github.com/org/repo/pull/42"
```

### Security scan

Define a security-focused workflow in `apm.yml`:

```yaml
scripts:
  security:
    description: "Security vulnerability scan"
    prompt: .apm/prompts/security-scan.prompt.md
    runtime: copilot
```

Then run it:

```bash
apm run security --param target_dir="src/"
```

### Multi-runtime setup

Use different runtimes for different tasks:

```yaml
scripts:
  review: "copilot --full-auto -p code-review.prompt.md"
  summarize: "llm summarize.prompt.md -m github/gpt-4o-mini"
  debug: "RUST_LOG=debug codex debug-analysis.prompt.md"
```

```bash
apm run review --param files="src/"
apm run summarize --param scope="recent-changes"
```

## Troubleshooting

**Runtime not found**: Run `apm runtime list` to verify installation. Re-run `apm runtime setup <name>` if needed.

**Command not found after setup**: Ensure the runtime binary is on your PATH. For Copilot CLI, verify Node.js v22+ is installed. For LLM, ensure the Python virtual environment is active.

**No scripts available**: Run `apm list` to check. If empty, either define scripts in `apm.yml` or install a package that includes `.prompt.md` files.
