---
title: "APM in CI/CD"
description: "Automate APM install in GitHub Actions, Azure Pipelines, and other CI systems."
sidebar:
  order: 1
---

APM integrates into your CI/CD pipeline to ensure agent context is always up to date.

## GitHub Actions

Use the official [apm-action](https://github.com/microsoft/apm-action) to install APM and run commands in your workflows:

```yaml
# .github/workflows/apm.yml
name: APM
on:
  push:
    branches: [main]
  pull_request:

jobs:
  install:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install APM packages
        uses: microsoft/apm-action@v1
        # Optional: add compile: true if targeting Codex, Gemini,
        # or other tools without native APM integration
```

### Private Dependencies

For private repositories, pass a token via the workflow `env:` block. See the [Authentication guide](../../getting-started/authentication/) for all supported tokens and priority rules.

```yaml
      - name: Install APM packages
        uses: microsoft/apm-action@v1
        env:
          GITHUB_APM_PAT: ${{ secrets.APM_PAT }}
```

### Verify Compiled Output (Optional)

If your project uses `apm compile` to target tools like Cursor, Codex, or Gemini, add a check to ensure compiled output stays in sync:

```yaml
      - name: Check for drift
        run: |
          apm compile
          if [ -n "$(git status --porcelain -- AGENTS.md CLAUDE.md)" ]; then
            echo "Compiled output is out of date. Run 'apm compile' locally and commit."
            exit 1
          fi
```

This step is not needed if your team only uses GitHub Copilot and Claude, which read deployed primitives natively.

### Verify Deployed Primitives

To ensure `.github/`, `.claude/`, `.cursor/`, and `.opencode/` integration files stay in sync with `apm.yml`, add a drift check:

```yaml
      - name: Check APM integration drift
        run: |
          apm install
          if [ -n "$(git status --porcelain -- .github/ .claude/ .cursor/ .opencode/)" ]; then
            echo "APM integration files are out of date. Run 'apm install' and commit."
            exit 1
          fi
```

This catches cases where a developer updates `apm.yml` but forgets to re-run `apm install`.

:::tip[We dogfood this]
APM's own repo uses the `APM Self-Check` job in [`microsoft/apm`'s `ci.yml`](https://github.com/microsoft/apm/blob/main/.github/workflows/ci.yml) as a reference implementation for installing APM, running CI validation commands such as `apm audit --ci`, and checking for drift with `git status --porcelain`. Use it as a practical example when wiring these checks into your own workflow.
:::

## Azure Pipelines

```yaml
steps:
  - script: |
      curl -sSL https://aka.ms/apm-unix | sh
      export PATH="$HOME/.apm/bin:$PATH"
      apm install
      # Optional: only if targeting Codex, Gemini, or similar tools
      # apm compile
    displayName: 'APM Install'
    env:
      ADO_APM_PAT: $(ADO_PAT)
```

### ADO with AAD bearer (no PAT)

In orgs that disable PAT creation, use a Workload Identity Federation (WIF) service connection and let APM consume the `az` session inherited from `AzureCLI@2`. Do NOT set `ADO_APM_PAT` -- APM falls back to the bearer cleanly only when no PAT env var is present.

```yaml
steps:
  - task: AzureCLI@2
    displayName: 'APM Install (AAD bearer)'
    inputs:
      azureSubscription: 'my-wif-service-connection'
      scriptType: bash
      scriptLocation: inlineScript
      inlineScript: |
        curl -sSL https://aka.ms/apm-unix | sh
        export PATH="$HOME/.apm/bin:$PATH"
        apm install
```

For GitHub Actions targeting ADO repos, use [`azure/login@v2`](https://github.com/marketplace/actions/azure-login) with OIDC federated credentials so `az` is signed in before `apm install` runs:

```yaml
permissions:
  id-token: write
  contents: read

jobs:
  install:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - uses: microsoft/apm-action@v1
        # Do not set ADO_APM_PAT -- APM picks up the az session.
```

See [Authentication: AAD bearer tokens](../../getting-started/authentication/#authenticating-with-microsoft-entra-id-aad-bearer-tokens) for resolution precedence and verbose output.

## General CI

For any CI system with Python available:

```bash
pip install apm-cli
apm install
# Optional: only if targeting Codex, Gemini, or similar tools
# apm compile --verbose
```

## Governance with `apm audit`

`apm audit --ci` verifies lockfile consistency in CI (6 baseline checks, no configuration). Add `--policy org` to enforce organizational rules (16 additional checks). For full setup including SARIF integration and GitHub Code Scanning, see the [CI Policy Enforcement guide](../../guides/ci-policy-setup/).

For content scanning and hidden Unicode detection, `apm install` automatically blocks critical findings. Run `apm audit` for on-demand reporting. See [Governance](../../enterprise/governance-guide/) for the full governance model.

## Pack & Distribute

Use `apm pack` in CI to build a distributable bundle once, then consume it in downstream jobs without needing APM installed.

### Pack in CI (build once)

```yaml
- uses: microsoft/apm-action@v1
  with:
    pack: true
- uses: actions/upload-artifact@v4
  with:
    name: agent-config
    path: build/*.tar.gz
```

### Pack as standalone plugin

```yaml
# Export as standalone plugin
- run: apm pack --format plugin
- uses: actions/upload-artifact@v4
  with:
    name: plugin-bundle
    path: build/*.tar.gz
```

### Consume in another job (no APM needed)

```yaml
- uses: actions/download-artifact@v4
  with:
    name: agent-config
- run: tar xzf build/*.tar.gz -C ./
```

Or use the apm-action restore mode to unpack a bundle directly:

```yaml
- uses: microsoft/apm-action@v1
  with:
    bundle: ./agent-config.tar.gz
```

See the [Pack & Distribute guide](../../guides/pack-distribute/) for the full workflow.

## Best Practices

- **Pin APM version** in CI to avoid unexpected changes: `pip install apm-cli==0.7.7`
- **Commit `apm.lock.yaml`** so CI resolves the same dependency versions as local development
- **Commit `.github/`, `.claude/`, `.cursor/`, and `.opencode/` deployed files** so contributors and cloud-based Copilot get agent context without running `apm install`
- **If using `apm compile`** (for Codex, Gemini), run it in CI and fail the build if the output differs from what's committed
- **Use `GITHUB_APM_PAT`** for private dependencies; never use the default `GITHUB_TOKEN` for cross-repo access
