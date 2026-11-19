---
title: "Migration paths"
description: "Adopting APM, upgrading across versions, switching compile strategies, and migrating between targets."
sidebar:
  order: 6
---

This page covers migrations you hit while adopting APM or upgrading the CLI. For first-time setup of a brownfield project, start with [Existing Projects](../../getting-started/migration/) and come back here for upgrade-time issues.

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

See [`apm init`](../../reference/cli/init/) and [`apm install`](../../reference/cli/install/) for full flag references.

## 2. From `awd-cli` (previous project name)

APM was previously distributed as `awd-cli` with an `awd` binary. There is no compatibility shim: the only installed entrypoint is `apm`.

Migration steps:

1. Uninstall the old binary:

   ```bash
   pip uninstall awd-cli
   # or whatever installer you used
   which awd && rm "$(which awd)"
   ```

2. Install APM via the documented [installation flow](../../getting-started/installation/).

3. In your scripts, CI workflows, and docs, replace `awd ` with `apm `. The subcommand surface (`init`, `install`, `compile`, `run`, `audit`) is the same.

4. Manifest and lockfile files keep their names (`apm.yml`, `apm.lock.yaml`). If your repo still has legacy `awd.yml` / `awd.lock.yaml`, rename them:

   ```bash
   git mv awd.yml apm.yml
   git mv awd.lock.yaml apm.lock.yaml
   apm install                     # regenerates lockfile against current resolver
   ```

[!] After renaming, regenerate the lockfile rather than editing it by hand. Field names and resolution semantics may have shifted between awd-cli and APM releases.

## 3. Lockfile schema upgrades

The current lockfile schemas are `lockfile_version: "1"` for plain Git projects and `"2"` when registry or Git semver resolution fields are present. When APM bumps this, an older binary reading a newer lockfile will refuse to proceed and name the versions it supports.

```text
Unsupported lockfile version '2'; supported versions: 1
```

Decision matrix:

| Situation                                  | Action                                  |
|--------------------------------------------|-----------------------------------------|
| Newer lockfile, older binary               | Upgrade the binary, then re-run install |
| Older lockfile, newer binary               | Run `apm install` to migrate in place   |
| Lockfile is corrupted or hand-edited       | Delete it, run `apm install` to regen   |
| Resolver semantics changed across versions | Delete + regen, then audit the diff     |

[i] Migrating in place preserves resolved versions where compatible. Deleting and regenerating re-resolves from `apm.yml` and may bump transitive dependencies. Review the diff before committing.

A lockfile whose `apm_version` is newer than the running APM is refused too; see [Lockfile written by a newer APM](../install-failures/#lockfile-written-by-a-newer-apm).

Schema details: [Lockfile spec](../../reference/lockfile-spec/).

### Mixed-case GitHub package paths

APM keeps GitHub lock and deduplication identity lowercase, but preserves the
repository display spelling from `apm.yml` in `apm_modules/` and generated
links. After upgrading, run:

```bash
apm install --verbose
apm audit --ci
```

One stale case-only package directory is renamed transactionally and appears in
verbose output. APM-managed links are rewritten; files you authored remain
untouched.

If install reports multiple package directories for one dependency:

1. Stop and back up every named directory.
2. Compare the directories and the matching `apm.yml` / `apm.lock.yaml` entry.
3. Keep the directory whose spelling matches `apm.yml`. Remove another only
   after confirming it is a duplicate and contains no unique edits.
4. Run `apm install --verbose`, then `apm audit --ci`.

An interrupted case-only rename can leave a hidden
`.apm-case-migration-<id>` entry beside the package directory. Inspect its
contents before renaming it back or removing it; APM does not delete an
unverified recovery entry automatically.

### Rejected dependency aliases

This repair changes no CLI commands or flags; it intentionally tightens
invalid-input handling. After whitespace trimming, exactly the bare tokens
`.` and `..` are newly rejected aliases. Separators (`/`, `\`) and percent
escapes were already invalid. `.safe`, `safe.`, `foo..bar`, and `my-skill.v2`
remain valid; see the
[alias field reference](../../reference/manifest-schema/#412-object-form).

Alias destinations must resolve strictly beneath `apm_modules/`; symlinks
resolving to that root or outside it fail.

1. Replace `alias: .` or `alias: ..` in the declaring `apm.yml` with
   `alias: my-skill.v2`. For unsafe symlink destinations, choose a separate
   package directory without removing the symlink target. Keep source paths,
   including local `../` paths, unchanged.
2. Run `apm install`. Review the lockfile, installed contents, and deployment
   changes before committing; do not assume prior metadata is repaired.

Never remove or prune a rejected alias path: it can refer to `apm_modules/`
or its parent.

The optional lock-entry `alias` fixes lost placement during replay and cleanup
without changing `lockfile_version`. Reinstall older aliased dependencies and
review this metadata. Older readers may preserve the field without honoring
placement; an absent alias never uses inventory `name`.

Opting into the [0.1.41 manifest `$schema`](../../reference/manifest-schema/#2-document-structure)
requires a client supporting that exact identity. Older clients lacking it
fail closed; this explicit opt-in is not backward-compatible.

## 4. Compile strategy migration

The compile step writes root context files (e.g. `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`). `compilation.strategy` selects a single-file (monolithic) layout or a per-directory (distributed) layout.

To switch:

1. Set `compilation.strategy` (`distributed` or `single-file`) in `apm.yml`.
2. Run `apm compile --clean` to materialize the new layout. Switching to `single-file` does not remove nested generated files (for example `src/AGENTS.md`); review and delete them by hand.
3. `git status` -- review added/removed files and commit.

[!] `--clean` never deletes hand-authored files, and compile keeps a hand-authored nested `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md` with a warning. Author content belongs in source packages.

Reference: [`apm compile`](../../reference/cli/compile/) and [`apm prune`](../../reference/cli/prune/).

## 5. Target migration

### Adding a target

Adding a new target (e.g. Cursor on a Copilot-only project) is non-destructive:

```bash
apm install --target cursor       # one-shot
# or persist in apm.yml under `targets:` and run `apm install`
apm compile
```

Existing target output is untouched. The new target's directory is created fresh.

### Removing a target

1. Remove the target from `apm.yml` (`targets:` field).
2. Run `apm install`. APM removes the deployed files for the dropped target.
3. Run `apm compile` to confirm the project is clean.

[!] This cleanup only removes files APM tracked in `apm.lock.yaml`. Files you placed by hand inside a target directory remain. Review `git status` afterwards.

Discovery: `apm targets` lists every supported target on the current binary.

### Upgrading to 0.37

- Replace singular `target:` with the `targets:` value its warning prints, delete `compilation.target`, and replace the deprecated `vscode` target with `copilot`. See [Common errors](../common-errors/).
- Copilot project MCP servers move to root `.mcp.json`, which Claude Code also reads (it ignores Copilot `tools` allow-lists). `apm install` moves the entries APM wrote to `.github/mcp.json` or `.vscode/mcp.json` and keeps entries you edited; review static `env:` values, then commit.
- With `default_host:` set, shorthand dependencies resolve against it; write `github.com/owner/repo` for GitHub packages.
- If several manifests narrow one dependency with different `skills:` or `targets:` and an older APM recorded a pairing that differs from what 0.37 resolves, `apm install --frozen` fails until you run `apm install` once and commit `apm.lock.yaml`.

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

See [Registries guide — pitfalls](../../guides/registries/#pitfalls) for env-var typos and name-sanitization collisions.

## 7. Marketplace switchover (hand-rolled MCP -> APM-managed)

If your project has a hand-edited `.mcp.json` (or `.vscode/mcp.json`) declaring servers directly:

1. Move each server into `dependencies.mcp` in `apm.yml`. APM resolves servers from the MCP registry (or as self-defined entries) and wires them per target on install.
2. Run `apm install`. APM rewrites the target-specific MCP config.
3. Diff the generated MCP config against your previous hand-rolled version and reconcile any custom env vars or args using the marketplace package's documented inputs.
4. Delete the legacy hand-rolled config once the APM-managed version is verified. Exception: remove a hand-written root `.mcp.json` entry that shares a server name before step 2; APM preserves a differing entry it did not write and reports a conflict.

For publishing your own marketplace entries, see [Publish to a marketplace](../../producer/publish-to-a-marketplace/).

## 8. Breaking-change checklist when upgrading APM

Before bumping `apm` across major or minor versions in a project that other people depend on:

- Read every `CHANGELOG.md` entry between `<your-version>` and `<target>`. Pay attention to `### Changed`, `### Removed`, `### Security`.
- Install the new binary in a scratch shell first (don't replace your working binary yet).
- Run `apm audit --ci` against the new binary in your repo to surface drift between the previous lockfile/output and what the new resolver produces.
- Run `apm install && apm compile` with the new binary. Inspect `git status` for unexpected churn.
- Run your project's tests and any agent-driven workflows that depend on the compiled output.
- If anything looks wrong, downgrade and open an issue. Do not commit a half-migrated lockfile.

## Related

- [Common errors](../common-errors/) -- diagnostic flowcharts for failures hit during migration.
- [Lockfile spec](../../reference/lockfile-spec/) -- schema reference.
- [`apm init`](../../reference/cli/init/), [`apm install`](../../reference/cli/install/), [`apm compile`](../../reference/cli/compile/), [`apm prune`](../../reference/cli/prune/).
