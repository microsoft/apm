---
title: "GitHub Rulesets"
description: "Enforce AI agent configuration governance using APM with GitHub branch protection and Rulesets."
sidebar:
  order: 5
---

GitHub Rulesets and branch protection rules can require status checks before merging. APM commands like `apm install`, `apm compile`, and `apm unpack` already block critical hidden-character findings automatically. `apm audit` adds structured reporting (SARIF, JSON, markdown) and exit codes (**0** = clean, **1** = critical, **2** = warnings) for CI integration. `apm audit --ci` verifies lockfile consistency, and `--policy org` enforces organizational rules.

## How It Works

The workflow is straightforward:

1. `apm install` runs in the workflow and blocks critical findings automatically.
2. `apm audit` scans installed packages and produces reports (SARIF for GitHub Code Scanning, exit codes for status checks).
3. You configure this workflow as a required status check in branch protection or Rulesets.
4. PRs that introduce content issues are blocked from merging.

This turns APM from a development convenience into an enforceable policy.

## Setup

### Step 1: Create the GitHub Actions Workflow

Add a workflow file at `.github/workflows/apm-audit.yml`:

```yaml
# .github/workflows/apm-audit.yml
name: APM Audit
on:
  pull_request:

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install & audit
        uses: microsoft/apm-action@v1
        with:
          audit-report: true
        env:
          GITHUB_APM_PAT: ${{ secrets.APM_PAT }}
```

The `GITHUB_APM_PAT` secret is only required if your `apm.yml` references private repositories. For public dependencies you can omit it.

### Step 2: Add the Required Status Check

1. Go to your repository **Settings** > **Rules**.
2. Select an existing branch ruleset or create a new one targeting your default branch.
3. Enable **Require status checks to pass** and add `APM Audit` (the workflow job name) as a required check.

Alternatively, in classic branch protection rules under **Settings** > **Branches** > **Branch protection rules**, enable **Require status checks to pass before merging** and search for `APM Audit`.

Once configured, any PR that introduces content issues detected by `apm audit` will fail the check.

## What It Catches

`apm audit` operates in three modes, each adding more checks:

**Content scanning** (`apm audit`):
- **Critical: Hidden Unicode characters** -- tag characters (U+E0001-E007F), bidi overrides (U+202A-202E, U+2066-2069), and SMP variation selectors. Exit code **1**.
- **Warning: Zero-width and invisible characters** -- zero-width spaces/joiners, mid-file BOM, soft hyphens. Exit code **2**. These are suspicious but not attack vectors.

**CI baseline checks** (`apm audit --ci`) -- adds lockfile verification on top of content scanning. See [policy-reference: Check reference](../../enterprise/policy-reference/#check-reference) for the canonical list of baseline and policy checks. In `--ci` mode, exit codes are binary: **0** = pass, **1** = fail. Warning-level characters do not fail CI.

**Policy enforcement** (`apm audit --ci --policy org`) -- adds organizational rules:
- **Approved/denied sources** -- restrict which repositories packages can come from
- **MCP transport controls** -- allow/deny transport types, trust settings for transitive MCP
- **Manifest requirements** -- enforce required fields, content types, scripts
- **Compilation rules** -- target and strategy constraints
- **Unmanaged file detection** -- flag files in integration directories not tracked by the lockfile

For full setup instructions, see the [CI Policy Enforcement](../../guides/ci-policy-setup/) guide. For the complete policy schema, see the [Policy Reference](../../enterprise/policy-reference/).

## Governance Levels

| Level | Description | Status |
|-------|-------------|--------|
| 1 | `apm audit` as a required status check (content scanning: critical=exit 1, warning=exit 2) | Available |
| 2 | `apm audit --ci` with lockfile verification (binary pass/fail, warnings do not block) | Available |
| 3 | `apm audit --ci --policy org` with organization policy enforcement | Available |
| 4 | GitHub recommends apm-action for agent governance | Future |
| 5 | Native Rulesets UI for agent configuration policy | Future |

Levels 1-3 are fully functional today. See the [CI Policy Enforcement](../../guides/ci-policy-setup/) guide for step-by-step setup. Levels 4-5 represent deeper GitHub platform integration that would reduce setup friction.

## Combining with Other Checks

APM audit complements your existing CI checks -- it does not replace them. A typical PR pipeline might include:

- **Linting and formatting** -- code style enforcement
- **Unit and integration tests** -- functional correctness
- **Security scanning** -- vulnerability detection
- **APM audit** -- content scanning, lockfile verification, and policy enforcement

Each check has a distinct purpose. APM audit focuses on AI agent configuration integrity -- from hidden Unicode detection to organizational policy compliance.

## Customizing the Workflow

### Running Audit Alongside Compile

You can combine audit with compilation to catch both governance violations and output drift in a single workflow:

```yaml
jobs:
  apm:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: APM checks
        uses: microsoft/apm-action@v1
        with:
          compile: true
          audit-report: true
        env:
          GITHUB_APM_PAT: ${{ secrets.APM_PAT }}
```

### Separate Jobs for Granular Status

If your project uses `apm compile` (for Codex, Gemini, or other tools whose instructions require compilation), you can add audit and compile as separate required checks:

```yaml
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
        with:
          audit-report: true
        env:
          GITHUB_APM_PAT: ${{ secrets.APM_PAT }}

  compile:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
        with:
          compile: true
```

This lets you require both `audit` and `compile` as independent status checks in your ruleset. The compile job is only needed if your project targets tools that require compiled instruction files.

## Troubleshooting

### Audit Fails on a Clean PR

If `apm audit` fails on a PR that did not touch agent config, run `apm install && apm audit` locally on the base branch to confirm, then commit the fix.

### Status Check Not Appearing in Rulesets

The status check name must match the **job name** in your workflow file (e.g., `audit`), not the workflow name. Run the workflow at least once so GitHub registers the check name, then add it to your ruleset.

## Related

- [CI Policy Enforcement](../../guides/ci-policy-setup/) -- step-by-step CI setup for policy enforcement
- [Governance](../../enterprise/governance-guide/) -- conceptual overview, bypass contract, and rollout playbook
- [Policy Reference](../../enterprise/policy-reference/) -- full `apm-policy.yml` schema reference
- [CI/CD Pipelines](../ci-cd/) -- general CI integration guide
- [Manifest Schema](../../reference/manifest-schema/) -- manifest and lock file reference
