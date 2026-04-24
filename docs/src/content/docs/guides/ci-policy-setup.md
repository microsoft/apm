---
title: CI Policy Enforcement
sidebar:
  order: 8
---

:::caution[Experimental Feature]
Policy enforcement (`apm audit --ci --policy`) is an early preview for testing and feedback. The policy schema, check behavior, and inheritance model may change based on community input. Do not use as a production governance gate without understanding that breaking changes are possible in upcoming releases.
:::

Set up automated policy enforcement so every pull request is checked against your organization's governance rules.

## Prerequisites

- An organization on GitHub with repositories using APM
- `apm audit --ci` runs 7 baseline consistency checks with no configuration
- `apm audit --ci --policy org` adds 17 policy checks defined in `apm-policy.yml`

For the full policy schema, see the [Policy Reference](../../enterprise/policy-reference/).

## Step 1: Create the org policy

Create `apm-policy.yml` in your org's `.github` repository. APM auto-discovers this file when `--policy org` is used.

```
your-org/.github/
└── apm-policy.yml
```

Start with a minimal policy:

```yaml
name: "Your Org Policy"
version: "1.0.0"
enforcement: block

dependencies:
  allow:
    - "your-org/**"
  deny:
    - "untrusted-org/**"

mcp:
  self_defined: warn
  transport:
    allow: [stdio, streamable-http]
```

Commit this to the default branch of `your-org/.github`.

## Step 2: Add baseline CI checks

Add `apm audit --ci` to your CI pipeline. This runs 6 lockfile consistency checks — no policy file needed:

```yaml
# .github/workflows/apm-policy.yml
name: APM Policy Compliance

on:
  pull_request:
    paths:
      - 'apm.yml'
      - 'apm.lock.yaml'
      - '.github/agents/**'
      - '.github/instructions/**'
      - '.github/hooks/**'
      - '.cursor/**'
      - '.claude/**'

jobs:
  apm-audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install APM
        run: curl -fsSL https://raw.githubusercontent.com/microsoft/apm/main/install.sh | bash

      - name: Run baseline checks
        run: apm audit --ci
```

This catches lockfile/manifest drift, missing files, and hidden Unicode — without any policy configuration.

## Step 3: Enable policy enforcement

Add `--policy org` to run the full 17 policy checks on top of baseline:

:::note
Since this release, `apm audit --ci` auto-discovers the org policy. `--policy org` remains valid as an explicit override; use `--no-policy` to skip discovery.
:::

```yaml
      - name: Run policy checks
        run: apm audit --ci --policy org --no-cache -f sarif -o policy-report.sarif
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Upload SARIF
        if: always()
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: policy-report.sarif
          category: apm-policy
```

Key flags:
- `--policy org` — auto-discovers `apm-policy.yml` from your org's `.github` repo
- `--no-cache` — fetches the latest policy (recommended for CI)
- `-f sarif -o policy-report.sarif` — generates SARIF for GitHub Code Scanning

The `GITHUB_TOKEN` provides read access to the `.github` repository for policy discovery.

A ready-to-use workflow template is available at [`templates/policy-ci-workflow.yml`](https://github.com/microsoft/apm/blob/main/templates/policy-ci-workflow.yml) in the APM repository.

## Step 4: Add repo-level overrides (optional)

Individual repositories can tighten the org policy by adding their own `apm-policy.yml` with `extends: org`:

```yaml
# repo-level apm-policy.yml
name: "Frontend Team Policy"
version: "1.0.0"
extends: org

dependencies:
  deny:
    - "legacy-org/**"  # Additional restriction

unmanaged_files:
  action: deny  # Stricter than org default
```

Child policies can only tighten constraints — never relax them. See [Inheritance](../../enterprise/policy-reference/#inheritance) for merge rules.

To use a repo-level policy file in CI:

```bash
apm audit --ci --policy ./apm-policy.yml
```

## Make it a required check

Configure the workflow as a required status check so PRs cannot merge with policy violations:

1. Go to repository (or org) **Settings → Rules → Rulesets**.
2. Create a ruleset targeting your protected branches.
3. Add **Require status checks to pass**.
4. Select the `apm-audit` job.

See [GitHub Rulesets](../../integrations/github-rulesets/) for org-wide setup.

## Alternative policy sources

### Local file

```bash
apm audit --ci --policy ./policies/apm-policy.yml
```

### URL

```bash
apm audit --ci --policy https://example.com/policies/apm-policy.yml
```

### Cross-org

```bash
apm audit --ci --policy enterprise-hub/.github
```

## Other CI systems

### GitLab CI

```yaml
apm-policy:
  image: python:3.12-slim
  script:
    - curl -fsSL https://raw.githubusercontent.com/microsoft/apm/main/install.sh | bash
    - apm audit --ci --policy org --no-cache
  rules:
    - changes:
        - apm.yml
        - apm.lock.yaml
```

### Azure Pipelines

```yaml
- task: Bash@3
  displayName: 'APM Policy Check'
  inputs:
    targetType: inline
    script: |
      curl -fsSL https://raw.githubusercontent.com/microsoft/apm/main/install.sh | bash
      apm audit --ci --policy org --no-cache
  env:
    GITHUB_TOKEN: $(GITHUB_TOKEN)
```

## What a violation looks like

When a developer adds a denied package to `apm.yml`:

```yaml
dependencies:
  apm:
    - untrusted-org/random-skills
```

The CI run fails with a clear pointer to the offending rule:

```
[x] Policy violation: dependency 'untrusted-org/random-skills' is denied by org policy
    Policy: contoso/.github/apm-policy.yml
    Rule:   dependencies.deny matches 'untrusted-org/**'
```

With `-f sarif -o results.sarif` and the GitHub Code Scanning upload step (Step 3 above), the same finding renders inline on the PR diff. The required status check stays red until the violation is resolved or the org policy is amended through its own change-management process.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All checks passed |
| 1 | One or more checks failed |

## Output formats

| Format | Flag | Use case |
|--------|------|----------|
| Text | `-f text` (default) | Human-readable Rich table |
| JSON | `-f json` | Machine-readable, tooling integration |
| SARIF | `-f sarif` | GitHub Code Scanning, VS Code |

Combine with `-o <path>` to write to a file.

## Related

- [Governance](../../enterprise/governance-guide/) -- conceptual overview, bypass contract, and rollout playbook
- [`apm-policy.yml`](../../enterprise/apm-policy/) -- mental model and how the policy file works
- [Policy Reference](../../enterprise/policy-reference/) -- full `apm-policy.yml` schema reference
- [GitHub Rulesets](../../integrations/github-rulesets/) -- enforce policy as a required status check
