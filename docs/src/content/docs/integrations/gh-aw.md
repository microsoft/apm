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
      target: copilot
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

The per-primitive path form is what makes `github/awesome-copilot/skills/review-and-refactor` work -- the awesome-copilot repo lays skills out at `/skills/<name>/`, not under `.apm/`. Use this form to consume skills from existing repositories without restructuring them. See [Package anatomy](../../concepts/package-anatomy/) for the full source-vs-output model.

**How it works:**

1. The gh-aw compiler detects the `shared/apm.md` import and adds a dedicated `apm` job to the compiled workflow.
2. The `apm` job runs `microsoft/apm-action` to install packages and uploads a bundle archive as a GitHub Actions artifact.
3. The agent job downloads and unpacks the bundle as pre-steps, making all primitives available at runtime.

Set the required `target:` input to the APM target that matches `engine:`. This is the only target signal: the shared workflow runs `apm-action` in isolated mode and intentionally ignores any `apm.yml` in the consumer repository. For example, use `target: copilot` with `engine: copilot`. Do not pass `all`: `apm-action` writes the value into the isolated `apm.yml`, where it is [deprecated](../../reference/manifest-schema/#36-target-and-targets) and degrades to auto-detection.

For no-App rows, `token-source` selects the package credential:

| Value | Behavior |
|---|---|
| `cascade` (default) | Selects the first configured value in `GH_AW_PLUGINS_TOKEN` -> `GH_AW_GITHUB_TOKEN` -> `GITHUB_TOKEN`. With no override it ends on the same built-in read token as `github-token`. On the shared workflow's GitHub-hosted runner, a configured override that is rejected does not fall through to another identity. |
| `github-token` | Deterministically selects only the ephemeral built-in token and fails if that identity is unavailable. The shared `apm` job grants it `contents: read`, so it can read private package paths in the current repository but not other private repositories. |

Use `token-source: github-token` when every private package in `packages:` is readable by the current repository token and you want to bypass configured cascade overrides. Private cross-repository packages require the default cascade with an authorized `GH_AW_PLUGINS_TOKEN` or `GH_AW_GITHUB_TOKEN`, or GitHub App credentials. App rows always use their minted installation token regardless of `token-source`. The `contents: read` permission is job-wide because GitHub Actions cannot scope token permissions per matrix row.

:::caution[Optional gh-aw telemetry credentials]
gh-aw v0.87.8 exposes `GH_AW_DEFAULT_OTLP_HEADERS` to the agent and MCP telemetry runtime when that enterprise secret is configured. This compiler-owned telemetry credential is separate from APM package authentication. Leave it unset unless agent-visible telemetry credentials are an accepted boundary.
:::

**Pinning the apm CLI version (optional):**

By default the import installs APM 0.28.0, the stable CLI version pinned by `shared/apm.md`. That version is action-tested with `microsoft/apm-action@v1.10.0` for explicit-target archive packing and multi-bundle restore. To pin the version explicitly, or install a different one, set the optional `apm-version` input. It is threaded into both the pack and restore steps so the version cannot skew between them, and it survives `gh aw update` (no need to hand-edit the vendored `shared/apm.md`):

```yaml
imports:
  - uses: shared/apm.md
    with:
      apm-version: '0.28.0'
      target: copilot
      packages:
        - microsoft/apm-sample-package
```

Use a bare semver tag (e.g. `'0.28.0'`). Pass `'latest'` to opt into floating to the newest release; omit the input entirely to keep the workflow's pinned default.

Copies vendored before this change default to APM 0.21.0, the repository's current CLI line when that default was selected. If a copy's `apm-action pin:` line reads `v1.4.2`, its target input applies only to packing and does not reach the isolated install. Replace older copies with the canonical file, set `target:`, and recompile; re-vendoring also moves the default to 0.28.0. Add `token-source: github-token` when private packages come from the current repository.

:::note[Isolated install by default]
`shared/apm.md` invokes `microsoft/apm-action` with `isolated: true`. Only the packages listed under `packages:` are installed -- any host-repo primitives under `.apm/` or `.github/` (instructions, prompts, skills, agents) are ignored and pre-existing primitive directories are cleared. To merge host-repo primitives with imported ones, use the [apm-action Pre-Step](#apm-action-pre-step) approach below, which leaves `isolated` at its default of `false`.
:::

:::caution[Deprecated: `dependencies:` frontmatter]
Earlier gh-aw versions accepted a top-level `dependencies:` field on the workflow. That form is deprecated and no longer supported -- migrate to the `imports: - uses: shared/apm.md` pattern shown above.
:::

:::tip[Vendor the canonical `shared/apm.md`]
`shared/apm.md` is a **local file** that gh-aw resolves at `.github/workflows/shared/apm.md` in your repository -- not a remote import. Two copies exist in the wild: one in [microsoft/apm](https://github.com/microsoft/apm/blob/main/.github/workflows/shared/apm.md) (canonical, current) and one in [github/gh-aw](https://github.com/github/gh-aw/blob/main/.github/workflows/shared/apm.md) (vendored, may lag).

To get the canonical version with multi-org GitHub App auth (`apps:`) and multi-bundle restore:

```bash
mkdir -p .github/workflows/shared
curl -sSL https://raw.githubusercontent.com/microsoft/apm/main/.github/workflows/shared/apm.md \
  > .github/workflows/shared/apm.md
gh aw compile
```

Check whether your vendored copy is current by comparing the `Source of truth:` and `apm-action pin:` lines near the top of the file with the canonical copy linked above.

Shared-workflow changes reach a consumer only after re-vendoring `shared/apm.md` and recompiling. CLI behavior changes, including [policy identity casing](../../reference/policy-schema/#identity-casing), additionally require a published CLI release and a runner upgrade.
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

1. Run `apm pack --format apm --archive` in your CI pipeline to produce a self-contained APM bundle (`.zip` by default; restorable via `unzip` or `apm-action` restore mode).
2. Distribute the bundle as a workflow artifact or commit it to the repository.
3. Reference the bundled primitives directly from `.github/agents/` in your workflow.

Bundles resolve full dependency trees ahead of time, so workflows need zero network access at runtime.

See the [CI/CD Integration guide](../ci-cd/) and [Pack and distribute](../../producer/pack-a-bundle/) for details on building and distributing bundles. For routing live install traffic through an enterprise proxy instead, see [Registry Proxy & Air-gapped](../../enterprise/registry-proxy/).

## Content Scanning

APM automatically scans dependencies for hidden Unicode characters during installation. Critical findings block deployment. This applies to both direct `apm install` and when gh-aw resolves packages via `shared/apm.md`.

For CI visibility into scan results (SARIF reports, step summaries), see the [CI/CD Integration guide](../ci-cd/#governance-with-apm-audit).

For details on what APM detects, see [Content scanning](../../enterprise/security/#content-scanning).

## Learn More

- [gh-aw Documentation](https://github.github.com/gh-aw/)
- [gh-aw Frontmatter Reference](https://github.github.com/gh-aw/reference/frontmatter/)
- [APM Compilation Guide](../../producer/compile/)
- [APM CLI Reference](../../reference/cli/install/)
- [CI/CD Integration](../ci-cd/)
