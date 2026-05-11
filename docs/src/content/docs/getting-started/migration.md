---
title: "Existing Projects"
description: "Add APM to a project that already has AI agent configuration, or migrate from npx skills add."
sidebar:
  order: 5
---

APM is additive. It never deletes, overwrites, or modifies your existing configuration files. Your current `.github/copilot-instructions.md`, `AGENTS.md`, `.claude/` config, `.cursor-rules` -- all stay exactly where they are, untouched.

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
`apm install owner/repo`. APM recognizes bare `skills/<name>/SKILL.md`
layouts (the [agentskills.io](https://agentskills.io) convention) as a
first-class package type; `apm.yml` is optional.

See [Package Types](../../reference/package-types/#skill-collection-skillsnameskillmd) for the full
skill collection layout reference.

## Next steps

- [Quick start](../quick-start/) -- first-time setup walkthrough
- [Dependencies](../../guides/dependencies/) -- managing external packages
- [Manifest schema](../../reference/manifest-schema/) -- full `apm.yml` reference
- [CLI commands](../../reference/cli/install/) -- complete command reference

## Deprecated targets

:::note[Deprecated]
`--target agents` is deprecated and maps to `copilot` (`.github/`), not `.agents/`. Use `--target copilot` for GitHub Copilot deployment, or `--target agent-skills` for cross-client `.agents/skills/` deployment. Removal in v1.0.
:::

## Skill routing convergence

:::caution[Behavior change]
Skills for **Copilot, Cursor, OpenCode, Codex, and Gemini** now deploy to `.agents/skills/` by default instead of per-client directories (`.github/skills/`, `.cursor/skills/`, `.gemini/skills/`, etc.). This matches the `.agents/` discovery path documented by all five clients and eliminates redundant copies when targeting multiple clients.

**Claude is unchanged** — its skills continue to deploy to `.claude/skills/`.

To restore the previous per-client layout, pass `--legacy-skill-paths` to any command, or set the `APM_LEGACY_SKILL_PATHS=1` environment variable.
:::

### Auto-migration of legacy lockfile state

When you upgrade APM and run `apm install`, the tool automatically detects legacy per-client skill paths (`.github/skills/`, `.cursor/skills/`, `.opencode/skills/`, `.gemini/skills/`) recorded in your `apm.lock.yaml` and migrates them to `.agents/skills/`.

**What happens:**
- Old per-client skill files are deleted after the new `.agents/skills/` files are written
- The lockfile is updated to reflect the new paths
- The migration is idempotent — running `apm install` again is a no-op
- Foreign / hand-authored skills outside the lockfile are never touched

**What does NOT migrate:**
- `.claude/skills/` — Claude is not part of the convergence
- `.codex/skills/` — Codex was already on `.agents/skills/` before this change
- Any file not tracked in `apm.lock.yaml`

**If a collision is detected** (e.g., a foreign file already exists at the destination `.agents/skills/` path with different content), the migration aborts entirely with a clear error. Use `--legacy-skill-paths` to skip migration and keep per-client paths.

### CI / automation

The first `apm install` after upgrading to this version will migrate legacy
per-client skill paths to `.agents/skills/` and update `apm.lock.yaml`. In
CI pipelines, this means the working tree will show:

- Deletions under `.github/skills/`, `.cursor/skills/`, `.opencode/skills/`,
  and/or `.gemini/skills/`
- Additions under `.agents/skills/`
- An updated `apm.lock.yaml`

To handle this in CI, either:

- Commit the migrated lockfile and `.agents/skills/` directory, then update
  your CI to expect the new layout, OR
- Set `APM_LEGACY_SKILL_PATHS=1` in your CI environment to defer the
  migration until you are ready to update the lockfile in a controlled
  commit.
