---
title: "Migration paths"
description: "Adopting APM, upgrading across versions, switching compile strategies, and migrating between targets."
sidebar:
  order: 6
---

This page covers migrations you hit while adopting APM or upgrading the CLI. For first-time setup of a brownfield project, start with [Existing Projects](../getting-started/migration/) and come back here for upgrade-time issues.

[i] Throughout: replace `<your-version>` with the version you currently have installed (`apm --version`) and `<target>` with the version you are moving to.

## 1. From hand-managed `.github/copilot-instructions.md`

Your existing instruction files are not touched by APM. `apm init` is additive: it writes `apm.yml` next to whatever already exists.

```bash
apm init                          # creates apm.yml; never edits existing files
apm install                       # resolves dependencies, writes apm.lock.yaml
apm compile                       # materializes target-specific output
git add apm.yml apm.lock.yaml
git commit -m "Adopt APM"
```

[+] Hand-written `.github/copilot-instructions.md`, `AGENTS.md`, `.cursor/rules`, `.claude/` configs continue to work side-by-side with APM-managed files.

[!] Do not commit `apm_modules/` -- add it to `.gitignore`. The lockfile is the reproducibility contract, not the installed tree.

See [`apm init`](../reference/cli/init/) and [`apm install`](../reference/cli/install/) for full flag references.

## 2. From `awd-cli` (previous project name)

APM was previously distributed as `awd-cli` with an `awd` binary. There is no compatibility shim: the only installed entrypoint is `apm`.

Migration steps:

1. Uninstall the old binary:

   ```bash
   pip uninstall awd-cli
   # or whatever installer you used
   which awd && rm "$(which awd)"
   ```

2. Install APM via the documented [installation flow](../getting-started/installation/).

3. In your scripts, CI workflows, and docs, replace `awd ` with `apm `. The subcommand surface (`init`, `install`, `compile`, `run`, `audit`) is the same.

4. Manifest and lockfile files keep their names (`apm.yml`, `apm.lock.yaml`). If your repo still has legacy `awd.yml` / `awd.lock.yaml`, rename them:

   ```bash
   git mv awd.yml apm.yml
   git mv awd.lock.yaml apm.lock.yaml
   apm install                     # regenerates lockfile against current resolver
   ```

[!] After renaming, regenerate the lockfile rather than editing it by hand. Field names and resolution semantics may have shifted between awd-cli and APM releases.

## 3. Lockfile schema upgrades

The current lockfile schemas are `lockfile_version: "1"` for plain Git projects and `"2"` when registry or Git semver resolution fields are present. When APM bumps this, an older binary reading a newer lockfile (or vice versa) will refuse to proceed and print upgrade instructions.

```text
[x] apm.lock.yaml uses lockfile_version "2", this binary supports "1"
    [>] Upgrade APM: see https://...
```

Decision matrix:

| Situation                                  | Action                                  |
|--------------------------------------------|-----------------------------------------|
| Newer lockfile, older binary               | Upgrade the binary, then re-run install |
| Older lockfile, newer binary               | Run `apm install` to migrate in place   |
| Lockfile is corrupted or hand-edited       | Delete it, run `apm install` to regen   |
| Resolver semantics changed across versions | Delete + regen, then audit the diff     |

[i] Migrating in place preserves resolved versions where compatible. Deleting and regenerating re-resolves from `apm.yml` and may bump transitive dependencies. Review the diff before committing.

Schema details: [Lockfile spec](../reference/lockfile-spec/).

## 4. Compile strategy migration

The compile step writes per-target output (e.g. `.github/copilot-instructions.md`, `.claude/`, `.cursor/rules/`). Some targets support both a single-file (monolithic) layout and a per-primitive (distributed) layout.

To switch:

1. Run `apm prune` first to remove APM-managed output for the current strategy. This avoids leaving orphans when the layout changes.
2. Update the target configuration in `apm.yml` (see the target's reference page).
3. Run `apm compile` to materialize the new layout.
4. `git status` -- review added/removed files and commit.

[!] Hand-edited files inside an APM-managed output directory will be lost on recompile. APM owns the output tree; author content lives in source packages.

Reference: [`apm compile`](../reference/cli/compile/) and [`apm prune`](../reference/cli/prune/).

## 5. Target migration

### Adding a target

Adding a new target (e.g. Cursor on a Copilot-only project) is non-destructive:

```bash
apm install --target cursor       # one-shot
# or persist in apm.yml under `target:` and run `apm install`
apm compile
```

Existing target output is untouched. The new target's directory is created fresh.

### Removing a target

1. Remove the target from `apm.yml` (`target:` field).
2. Run `apm prune`. APM removes the deployed files for the dropped target.
3. Run `apm install && apm compile` to confirm the project is clean.

[!] `apm prune` only removes files APM tracked in `apm.lock.yaml`. Files you placed by hand inside a target directory remain. Review `git status` after pruning.

Discovery: `apm targets` lists every supported target on the current binary.

## 6. Default registry adoption (Git → registry routing)

Adopting a default registry changes how **existing** shorthand dependencies resolve. Entries like `microsoft/apm-sample-package#^1.0.0` that previously cloned from GitHub route to the configured registry instead. APM does not print a migration banner — failures show up as registry errors (`no versions`, `401`) on the next `apm install`.

Recommended rollout:

1. **Inventory** — list shorthand deps in the root `apm.yml` and in installed packages under `apm_modules/` that are not yet published to your registry.

2. **Pin Git-only deps** before enabling the default:

   ```yaml
   dependencies:
     apm:
       - git: https://github.com/microsoft/apm-sample-package.git
         ref: v1.0.0
   ```

3. **Enable gradually** — start with `registries:` in `apm.yml` without `default:`, publish packages, then set `registry.<name>.default true` or `registries.default` once shorthand deps exist on the registry.

4. **Verify lockfile** — after the first install with the default, confirm each entry has the intended `source:` (`registry` vs git commit SHA):

   ```bash
   apm install
   grep -E 'source:|repo_url:' apm.lock.yaml
   ```

5. **Publish or remove** — deps that must stay on Git use `- git:`; deps moving to the registry need a published version before the team enables the default org-wide.

See [Registries guide — pitfalls](../guides/registries/#pitfalls) for env-var typos and name-sanitization collisions.

## 7. Marketplace switchover (hand-rolled MCP -> APM-managed)

If your project has a hand-edited `.mcp.json` (or VS Code `mcp.json`) declaring servers directly:

1. Move each server into the `mcp:` block of `apm.yml`. APM resolves servers from the marketplace and wires them per target on install.
2. Run `apm install`. APM rewrites the target-specific MCP config.
3. Diff the generated MCP config against your previous hand-rolled version and reconcile any custom env vars or args using the marketplace package's documented inputs.
4. Delete the legacy hand-rolled config once the APM-managed version is verified.

For publishing your own marketplace entries, see [Publish to a marketplace](../producer/publish-to-a-marketplace/).

## 8. Breaking-change checklist when upgrading APM

Before bumping `apm` across major or minor versions in a project that other people depend on:

- Read every `CHANGELOG.md` entry between `<your-version>` and `<target>`. Pay attention to `### Changed`, `### Removed`, `### Security`.
- Install the new binary in a scratch shell first (don't replace your working binary yet).
- Run `apm audit --ci` against the new binary in your repo to surface drift between the previous lockfile/output and what the new resolver produces.
- Run `apm install && apm compile` with the new binary. Inspect `git status` for unexpected churn.
- Run your project's tests and any agent-driven workflows that depend on the compiled output.
- If anything looks wrong, downgrade and open an issue. Do not commit a half-migrated lockfile.

## Related

- [Common errors](./common-errors/) -- diagnostic flowcharts for failures hit during migration.
- [Lockfile spec](../reference/lockfile-spec/) -- schema reference.
- [`apm init`](../reference/cli/init/), [`apm install`](../reference/cli/install/), [`apm compile`](../reference/cli/compile/), [`apm prune`](../reference/cli/prune/).
