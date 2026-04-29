---
title: "Existing Projects"
description: "Add APM to a project that already has AI agent configuration, or migrate from npx skills add."
sidebar:
  order: 5
---

APM is additive. It never deletes, overwrites, or modifies your existing configuration files. Your current `.github/copilot-instructions.md`, `AGENTS.md`, `.claude/` config, `.cursor-rules` — all stay exactly where they are, untouched.

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

## Coming from `npx skills add`

APM is a drop-in replacement. The install gesture is identical, and you also
get a manifest, lockfile, and reproducible installs across machines.

```bash
# Install a whole skill bundle (equivalent to: npx skills add vercel-labs/agent-skills)
apm install vercel-labs/agent-skills

# Install a single skill from a bundle and persist the selection to apm.yml
apm install vercel-labs/agent-skills --skill deploy-to-vercel

# Subsequent bare apm install respects the persisted selection
apm install
```

The `--skill` flag is repeatable. Your selection is written to `apm.yml` and
`apm.lock.yaml` so the exact subset is reproducible on every machine.

```bash
# Pick two skills, then reset to all
apm install vercel-labs/agent-skills --skill deploy-to-vercel --skill preview
apm install vercel-labs/agent-skills --skill '*'   # back to full bundle
```

Any public repo that works with `npx skills add owner/repo` also works with
`apm install owner/repo`. APM recognises bare `skills/<name>/SKILL.md`
layouts (the [agentskills.io](https://agentskills.io) convention) as a
first-class package type; `apm.yml` is optional.

See [Package Types](../../reference/package-types/#skill-collection-skillsnameskillmd) for the full
skill collection layout reference.

## Next steps

- [Quick start](../quick-start/) — first-time setup walkthrough
- [Dependencies](../../guides/dependencies/) — managing external packages
- [Manifest schema](../../reference/manifest-schema/) — full `apm.yml` reference
- [CLI commands](../../reference/cli-commands/) — complete command reference
