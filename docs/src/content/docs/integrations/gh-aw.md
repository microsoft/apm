---
title: "GitHub Agentic Workflows"
description: "How APM integrates with GitHub Agentic Workflows for automated agent pipelines."
sidebar:
  order: 2
---

[GitHub Agentic Workflows](https://github.github.com/gh-aw/) (gh-aw) lets you write repository automation in markdown and run it as GitHub Actions using AI agents. APM and gh-aw have a native integration: gh-aw recognizes APM packages as first-class dependencies.

## How They Work Together

| Tool | Role |
|------|------|
| **APM** | Manages the *context* your AI agents use -- skills, instructions, prompts, agents |
| **gh-aw** | Manages the *automation* that triggers AI agents -- event-driven workflows |

APM defines **what** agents know. gh-aw defines **when** and **how** they act.

## Integration Approaches

### Shared apm.md Import (Recommended)

gh-aw ships a [shared `apm.md` workflow component](https://github.github.com/gh-aw/reference/dependencies/) that turns APM packages into gh-aw dependencies. Import it in your workflow's frontmatter and pass the packages you want.

```yaml
---
on:
  pull_request:
    types: [opened]
engine: copilot

imports:
  - uses: shared/apm.md
    with:
      packages:
        - microsoft/apm-sample-package
        - github/awesome-copilot/skills/review-and-refactor
        - your-org/security-compliance#v1.4.0
---

# Code Review

Review the pull request using the installed coding standards and skills.
```

**Package reference formats:**

| Format | Description |
|---|---|
| `owner/repo` | Full APM package (skills/agents/instructions under `.apm/`) |
| `owner/repo/path/to/primitive` | Individual primitive (skill, instruction, plugin, etc.) from any repository, regardless of layout |
| `owner/repo#ref` or `owner/repo/path/to/primitive#ref` | Pinned to a tag, branch, or commit SHA, for either a full package or a specific primitive |

The per-primitive path form is what makes `github/awesome-copilot/skills/review-and-refactor` work -- the awesome-copilot repo lays skills out at `/skills/<name>/`, not under `.apm/`. Use this form to consume skills from existing repositories without restructuring them. See [Anatomy of an APM Package](../../introduction/anatomy-of-an-apm-package/) for the full source-vs-output model.

**How it works:**

1. The gh-aw compiler detects the `shared/apm.md` import and adds a dedicated `apm` job to the compiled workflow.
2. The `apm` job runs `microsoft/apm-action` to install packages and uploads a bundle archive as a GitHub Actions artifact.
3. The agent job downloads and unpacks the bundle as pre-steps, making all primitives available at runtime.

The APM compilation target is automatically inferred from the configured `engine:` field (`copilot`, `claude`, or `all` for other engines). No manual target configuration is needed.

Packages are fetched using gh-aw's cascading token fallback: `GH_AW_PLUGINS_TOKEN` -> `GH_AW_GITHUB_TOKEN` -> `GITHUB_TOKEN`.

:::note[Isolated install by default]
`shared/apm.md` invokes `microsoft/apm-action` with `isolated: true`. Only the packages listed under `packages:` are installed -- any host-repo primitives under `.apm/` or `.github/` (instructions, prompts, skills, agents) are ignored and pre-existing primitive directories are cleared. To merge host-repo primitives with imported ones, use the [apm-action Pre-Step](#apm-action-pre-step) approach below, which leaves `isolated` at its default of `false`.
:::

:::caution[Deprecated: `dependencies:` frontmatter]
Earlier gh-aw versions accepted a top-level `dependencies:` field on the workflow. That form is deprecated and no longer supported -- migrate to the `imports: - uses: shared/apm.md` pattern shown above.
:::

### apm-action Pre-Step

For more control over the installation process, use [`microsoft/apm-action@v1`](https://github.com/microsoft/apm-action) as an explicit workflow step. This approach runs `apm install` directly, giving you access to the full APM CLI. To also compile, add `compile: true` to the action configuration.

```yaml
---
on:
  pull_request:
    types: [opened]
engine: copilot

steps:
  - name: Install agent primitives
    uses: microsoft/apm-action@v1
    with:
      script: install
    env:
      GITHUB_TOKEN: ${{ github.token }}
---

# Code Review

Review the PR using the installed coding standards.
```

The repo needs an `apm.yml` with dependencies and `apm.lock.yaml` for reproducibility. The action runs as a pre-agent step, deploying primitives to `.github/` where the agent discovers them.

**When to use this over frontmatter dependencies:**

- Custom compilation options (specific targets, flags)
- Running additional APM commands (audit, preview)
- Workflows that need `apm.yml`-based configuration
- Debugging dependency resolution

## Using APM Bundles

For sandboxed environments where network access is restricted during workflow execution, use pre-built APM bundles:

1. Run `apm pack` in your CI pipeline to produce a self-contained bundle.
2. Distribute the bundle as a workflow artifact or commit it to the repository.
3. Reference the bundled primitives directly from `.github/agents/` in your workflow.

Bundles resolve full dependency trees ahead of time, so workflows need zero network access at runtime.

See the [CI/CD Integration guide](../ci-cd/) and [Pack & Distribute](../../guides/pack-distribute/) for details on building and distributing bundles. For routing live install traffic through an enterprise proxy instead, see [Registry Proxy & Air-gapped](../../enterprise/registry-proxy/).

## Content Scanning

APM automatically scans dependencies for hidden Unicode characters during installation. Critical findings block deployment. This applies to both direct `apm install` and when gh-aw resolves packages via `shared/apm.md`.

For CI visibility into scan results (SARIF reports, step summaries), see the [CI/CD Integration guide](../../integrations/ci-cd/#content-scanning-in-ci).

For details on what APM detects, see [Content scanning](../../enterprise/security/#content-scanning).

## Learn More

- [gh-aw Documentation](https://github.github.com/gh-aw/)
- [gh-aw Frontmatter Reference](https://github.github.com/gh-aw/reference/frontmatter/)
- [APM Compilation Guide](../../guides/compilation/)
- [APM CLI Reference](../../reference/cli-commands/)
- [CI/CD Integration](../ci-cd/)
