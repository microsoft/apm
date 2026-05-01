---
title: "Existing Projects"
description: "Add APM to a project that already has AI agent configuration."
sidebar:
  order: 5
---

APM is additive. It never deletes, overwrites, or modifies your existing configuration files. Your current `.github/copilot-instructions.md`, `AGENTS.md`, `.claude/` config, `.cursor-rules` -- all stay exactly where they are, untouched.

:::caution[Unreleased compile change]
`apm compile --target vscode` and `apm compile --target all` no longer write `.github/copilot-instructions.md`. Existing files stay in place, but APM will not regenerate that path.

Before: `apm compile --target vscode` generated `AGENTS.md`, `.github/` primitives, and `.github/copilot-instructions.md`.

After: `apm compile --target vscode` generates `AGENTS.md` and `.github/` primitives only.

To keep the last generated file from the previous commit:

```bash
git show HEAD~1:.github/copilot-instructions.md > .github/copilot-instructions.md
```

For CI, remove assertions that expect APM to regenerate `.github/copilot-instructions.md`, or commit the file and manage it as a normal repository-owned file.
:::

## Add APM in three steps

### 1. Initialize

Run `apm init` in your project root:

```bash
apm init
```

This creates an `apm.yml` manifest alongside your existing files. Nothing is deleted or moved.

### 2. Install packages

Add the shared packages your team needs:

```bash
apm install microsoft/copilot-best-practices
apm install your-org/team-standards
```

Each package brings in versioned, maintained configuration instead of stale copies. Your `apm.yml` tracks these as dependencies, and `apm.lock.yaml` pins exact versions.

### 3. Commit and share

```bash
git add apm.yml apm.lock.yaml
git commit -m "Add APM manifest"
```

Your teammates run `apm install` and get the same setup. No more copy-pasting configuration between repositories.

## What happens to your existing files?

They continue to work. APM-managed files coexist with manually-created ones. There is no conflict and no takeover.

Over time, you may choose to move manual configuration into APM packages for portability across repositories, but there is no deadline or requirement to do so. APM and manual configuration coexist indefinitely.

## Rollback

If you decide APM is not for you:

1. Delete `apm.yml` and `apm.lock.yaml`.
2. Your original files are still there, unchanged.

No uninstall script, no cleanup command. Zero risk.

## Next steps

- [Quick start](../quick-start/) — first-time setup walkthrough
- [Dependencies](../../guides/dependencies/) — managing external packages
- [Manifest schema](../../reference/manifest-schema/) — full `apm.yml` reference
- [CLI commands](../../reference/cli-commands/) — complete command reference
