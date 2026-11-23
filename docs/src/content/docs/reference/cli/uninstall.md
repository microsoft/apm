---
title: apm uninstall
description: Remove an APM package from the project
sidebar:
  order: 3
---

Remove one or more APM packages from `apm.yml`, the lockfile, `apm_modules/`, and every deployed primitive across all configured harnesses.

## Synopsis

```bash
apm uninstall [OPTIONS] PACKAGES...
```

## Description

`apm uninstall` removes selected packages and unused transitive dependencies.
File cleanup uses the lockfile's `deployed_files` list, preserving hand-authored
content. Global uninstalls also remove the Claude plugin declarations APM made.

## Arguments

| Argument | Description |
|---|---|
| `PACKAGES...` | One or more packages to remove. Accepts shorthand (`owner/repo`), HTTPS URL, SSH URL, FQDN, marketplace notation (`name@marketplace`), an exact declared local path, or the portable `_local/<name>` identifier printed for a direct local dependency with matching lock metadata. Required. A local bundle installed with `apm install <bundle>` is not in `apm.yml`: its path (as given to install) or installed name (the `--as` name, if any) removes the bundle's deployed hooks on every target, their scripts and lockfile claims, keeps its other files and says so, and is refused when it has no deployed hooks, matches several installed copies, or is mixed with other packages. A hook file that cannot be removed (for example a symlink) fails the command and stays recorded; when the hooks are removed but `apm_modules/.local-bundle-pending.json` cannot be updated (for example a locked file), the command fails and names the file, and running the same uninstall again finishes the update; a linked or unreadable hook config, or a lockfile APM cannot rewrite, refuses the uninstall before any change. An MCP server name is refused with the route that removes it: delete it under `dependencies.mcp` and run `apm install`, or uninstall the package that brings it in; a server from a local bundle cannot be uninstalled yet ([#3887](https://github.com/microsoft/apm/issues/3887)). |

## Options

| Option | Description |
|---|---|
| `--dry-run` | Show the removal plan without writes from the uninstall engine. `pre-uninstall` lifecycle scripts still run and may have side effects. Registry fallback and shared-slot survivor staging are skipped. |
| `-v, --verbose` | Show detailed removal information. |
| `--trust-lockfile` | Consent, for this run only, to delete a file that has no deployment evidence (no receipt, and the locked version cannot be rendered because its source is gone) when its bytes equal the hash `apm.lock.yaml` records. Every other file is kept and named. No other option implies it. |
| `-g, --global` | Remove from the user scope under `$APM_HOME` (default `~/.apm`) instead of the current project. Target deployment cleanup still uses target-specific home variables or the operating-system home. |

## Examples

Remove one package:

```bash
apm uninstall acme/my-package
```

Remove several at once:

```bash
apm uninstall org/pkg1 org/pkg2
```

Preview the uninstall engine's plan (pre-uninstall scripts still run):

```bash
apm uninstall acme/my-package --dry-run
```

Remove from the user scope:

```bash
apm uninstall -g acme/my-package
```

Remove a local dependency by copying the portable key from `apm deps list`:

```bash
apm deps list
apm uninstall "_local/my-local-package"
```

The same key works at user scope, so scripts do not need the absolute path used
when the local package was installed:

```bash
apm deps list -g
apm uninstall -g "_local/my-local-package"
```

Remove by marketplace name (resolved via lockfile, then registry):

```bash
apm uninstall my-plugin@official
```

Resolve via URL (same identity as the shorthand):

```bash
apm uninstall https://github.com/acme/my-package.git
```

## Behavior

What gets removed, in order:

1. Package folders under `apm_modules/`, including requested packages and
   transitive dependencies that no remaining package needs. A transitive
   dependency still required by a surviving package is preserved, including
   shared diamond dependencies. If a surviving package's manifest cannot be
   read, APM keeps every remaining candidate rather than guessing. Run again
   with `--verbose`, fix or restore the reported manifest, then retry.
   A requested direct dependency that another installed package still
   needs is removed from `apm.yml` only: it stays installed, its lockfile
   entry now names that package as its parent, and `apm install --frozen`
   keeps passing. The output says which package still needs it. A local
   (path) dependency also takes the lockfile identity it has under that
   package, the same entry `apm install` records, and keeps its deployment
   receipts. If the packages that need it cannot be read offline, or APM
   cannot prove it wrote the package's files, uninstall refuses before any
   change. A skill another package also ships takes the writer `apm install`
   would record; when that would change its bytes, or no receipt proves
   them, uninstall refuses before any change. Re-anchoring runs on its own:
   uninstall other packages in a separate command. A failed or interrupted
   re-anchor restores `apm.yml`, the lockfile and the receipts together.
   Now-empty `apm_modules/` parent directories are cleaned at this step.
2. Target-scoped files owned only by the removed packages, while lockfile
   ownership is still available for safe cleanup.
3. Package declarations in `apm.yml`.
4. Remaining files in the lockfile's `deployed_files` for the removed packages
   and pruned orphans, across target-owned folders such as `.github/`,
   `.claude/`, `.grok/`, and `.agents/`.
5. Hook entries inside `.claude/settings.json`, `.cursor/hooks.json`,
   `.gemini/settings.json`, and `.kiro/hooks/` that the removed packages
   contributed. Remaining packages -- including transitive dependencies still
   required by another package -- have their hook entries rebuilt from the
   post-removal lockfile. Remaining packages are rebuilt only into targets
   `apm.lock.yaml` records: when `apm.yml` now declares a target the project
   was not installed for, uninstall leaves it alone and says once to run
   `apm install`, which deploys and records it.
6. MCP and LSP servers contributed only by the removed packages. For current
   lockfiles, MCP cleanup touches only the runtimes recorded as owners in
   `mcp_target_servers`. An explicitly empty ownership map is a no-op. Older
   lockfiles adopt only self-defined entries that exactly match their stored
   configuration baseline. LSP cleanup works within the targets
   `lsp_target_servers` records, or the `targets:` in `apm.yml` when declared;
   a target saved with `apm config set target` or harness detection applies only when the
   lockfile records none. When another surviving package declares the same LSP
   server name, ownership transfers to that package instead of deleting the
   shared entry. Cleanup attempts every owning runtime before exiting nonzero on
   failure. Fix the reported configs, then run `apm install` to reconcile stale
   entries.
7. Lockfile entries. If no dependencies remain, `apm.lock.yaml` is deleted.
8. After lockfile processing, remove the removed packages' native plugin roots
   (`.agents/plugins/<plugin>/`, or `~/.agents/plugins/<plugin>/` with
   `--global`) and the settings entries APM declared for them. A key you set to
   `false`, your own entries and a root you edited are kept.

When uninstall rebuilds a surviving package's loose skills, it deploys only the
skills that package selects with `skills:`, as `apm install` does. APM records
those actual writes locally and binds them after the final lockfile update succeeds.
Later installs can update the survivor without treating its unchanged files as
user edits. Removed-output receipts are retired before survivor copies; failed
removals do not retire evidence. Receipt failures are reported, not ignored.

### Harness profiles

Claude Code profiles share one user manifest and lockfile. `apm uninstall -g`
removes a package's files and lockfile rows only under the selected
`CLAUDE_CONFIG_DIR` (or `~/.claude`). Files deployed to another profile stay.
If another profile still uses the package, it stays in `~/.apm/apm.yml` and
`apm_modules/`; uninstall it with that profile selected to remove it everywhere.
Hermes homes (`HERMES_HOME`) follow the same rule. Only the runtime whose
profiles share the package loses files: selecting a Claude profile leaves
`~/.hermes` (and other targets such as `~/.agents`) untouched, and the reverse.
The selected profile is matched by path; symlinks are not resolved.
Codex homes selected by `CODEX_HOME` (or `~/.codex`) follow the same rule for
Codex agents and hooks, and Copilot CLI homes selected by `COPILOT_HOME` (or
`~/.copilot`) for Copilot instructions, agents and hooks.

`Cleaned N stale deployed files for DEPENDENCY:` identifies the affected
dependency, not the deletion location, and lists each removed path below it (a
removed skill directory is listed once, with a trailing `/`). An absolute local
path in the header names the source package; cleanup removes its deployed
files, not source files.

Selection is atomic. If any requested identifier does not match a declaration,
the command exits nonzero before lifecycle scripts or filesystem writes run. No
matched package in the same invocation is removed. Fix the identifier and retry.

If deletion of a requested or orphan materialized directory fails, or APM
refuses an unsafe path that fails containment checks, uninstall exits 1 without
printing `Uninstall complete`. These deletions precede all target cleanup and
manifest or lockfile writes, so declarations, the on-disk lockfile, and deployed
ownership remain. Earlier deletions are not rolled back.

Fix permission or file-lock errors; for a containment refusal, correct the
unsafe path instead. Retry the same `apm uninstall` command, or restore declared
packages with `apm install` (`apm install --global` for user scope).

If replacing a shared local install slot fails, uninstall also retains manifest
and lockfile ownership. Resolve the filesystem problem and retry the same
command; `--verbose` includes the underlying error. Earlier deletions in a
multi-package request are not rolled back.

Uninstall deletes a removed package's files only with evidence that APM wrote
their current bytes: a local deployment receipt, or byte equality with what the
locked version renders. It renders before removing the package from
`apm_modules/`, fetching the locked commit when the checkout is gone; a lockfile
hash alone is never evidence. The same check covers files a surviving shared
package stops deploying because the removed package was the only one selecting
them (for example a skill only it chose from a shared bundle). If a file it
would delete was edited or has no evidence, uninstall exits 1 before changing
anything: `apm.yml`,
`apm.lock.yaml`, `apm_modules/` and every file stay as they were. It names each
file and why, then offers two remedies:

- Move aside any listed file that holds your edits, then run `apm install` to
  re-establish deployment evidence (once, with access to the package source),
  then rerun `apm uninstall`. Install rewrites a file it cannot prove it wrote.
- If the source is unreachable, move each listed file aside or delete it, then
  run `apm uninstall` to finish the uninstall.
- If the source is gone for good, rerun the same uninstall with
  `--trust-lockfile` (for example `apm uninstall owner/repo --trust-lockfile`);
  the remedy prints that exact command. It is offered only when the refusal
  marks at least one file `--trust-lockfile would delete it`.

The refusal marks each file `--trust-lockfile would delete it` or
`kept even with --trust-lockfile`. `--trust-lockfile` adds one lockfile-only
path: a file with no receipt whose locked version cannot be rendered is deleted
when its bytes equal the one hash `apm.lock.yaml` records for it. It prints each
such deleted path. The flag never deletes on the lockfile hash alone an edited
file, a file a render or receipt disagrees with, a file the lockfile records no
single hash for, an unreadable or symlinked file, or any file when receipt state
cannot be read (including a receipt store no installation marker binds); those
are kept and named. Receipt- or render-backed deletion still applies as without
the flag, so the flag is not a preservation guarantee. In a skill
directory, each eligible file is deleted and only the kept files stay. Kept
files do not fail the command, and the package leaves `apm.yml` and
`apm.lock.yaml`; a file that could not be deleted still fails it.

After upgrading, uninstall needs this evidence. If the source is unreachable,
use the second or third remedy. If a target-scoped file cannot be deleted, uninstall
lists the retained paths and exits before changing `apm.yml`. Files it already
removed are dropped from `apm.lock.yaml`, so the lockfile claims only what is
still on disk. Direct and orphan directories run first and may already be gone;
declarations remain. Resolve the listed files and retry the same uninstall
command.

A file the removed package shares with your project's own content or a local
bundle (`apm install <bundle>`) stays on disk and in `apm.lock.yaml` under that
owner. If uninstall reports a package failure, it exits nonzero instead of
printing `Uninstall complete`.

If a managed hook changes after the initial check or is beneath a symlinked
parent, uninstall preserves and lists the path. Package removal finishes, but the
command exits nonzero because hook cleanup is incomplete. Inspect or repair the
path before removing anything manually.

If safe LSP cleanup fails after package removal, uninstall exits nonzero,
preserves the conflicting configuration, and tells you to repair the path or
ownership conflict. Run `apm install` to reconcile project state, or
`apm install --global` after a global uninstall.

If updating native plugin settings fails, uninstall exits nonzero; package and
manifest removals may already have happened. APM restores the previous
`apm.lock.yaml`, so it still records the plugins to remove. Repair the reported
issue, then run `apm install` (`apm install --global` for user scope) with the
original `--target` selection to finish. Do not re-add removed package arguments. See
[Native plugins](../../../consumer/install-plugins/#native-plugins).

A package declared more than once -- in both `dependencies` and
`devDependencies`, or under two spellings such as `owner/repo` and its HTTPS
URL -- is one package: uninstall removes every declaration of it.

`_local/<name>` is resolved from the manifest and lockfile metadata that produced
the `apm deps list` row. APM does not reinterpret it as `owner/repo` or rebuild an
absolute path. If two declared local dependencies have the same portable key,
selection is ambiguous and exits nonzero without changes. Use one exact path
already declared in the relevant manifest (`apm.yml` or `$APM_HOME/apm.yml`,
default `~/.apm/apm.yml`); APM
never removes both or guesses, and diagnostics do not print declared paths.
When an exact path removes one declaration from a shared local install slot, APM
validates and stages the survivor before changing the manifest, then activates
it during cleanup.
If more than one declaration would remain in that slot, uninstall fails without
APM writes. Pass enough exact paths in one command that at most one declaration
remains.

When `apm.yml` declares the package in object form (`{name: my-plugin, marketplace: official}`), `apm uninstall my-plugin@official` selects that declaration directly. The marketplace name matches case-insensitively, and the lock row, `apm_modules/` checkout and deployed files are found through the lockfile. Neither the marketplace nor its catalog is contacted, with or without `--dry-run`.

If a marketplace ref cannot be resolved (neither the lockfile nor the registry has a matching entry), APM logs an error and aborts without changes. Use `owner/repo` notation to uninstall directly, or run `apm deps list` to find the canonical name.

### Supply-chain guard

When marketplace notation (`name@marketplace`) falls through to the registry (Stage 2), APM refuses any canonical the registry returns that is not already recorded in `apm.lock.yaml`. The refusal is reported as a warning naming the resolved canonical so you can decide whether to re-run with `apm uninstall owner/repo` directly. This prevents a poisoned marketplace registry from coercing APM into removing an unrelated installed package.

### `#ref` is not meaningful for `uninstall`

`apm install` accepts an optional `#ref` fragment (`apm install NAME@MKT#ref`) to pin a specific revision. `apm uninstall` identifies packages by canonical name only, so any `#ref` fragment supplied with marketplace notation (e.g. `my-plugin@official#v1.0.0`) is ignored.

### No-lockfile behavior

If `apm.lock.yaml` is not present, marketplace notation has no offline anchor: Stage 1 finds nothing, and the supply-chain guard cannot cross-check the registry result. APM still attempts registry resolution and proceeds if the canonical matches an entry in `apm.yml`, but this path has weaker integrity guarantees. A local `_local/<name>` key also has no persisted mapping without the lockfile, so use its exact declared path or run `apm install` to regenerate the lockfile first.

Without a lockfile, nothing proves which skill directories APM deployed, so uninstall (project and `-g`) removes no skill directories and prints a notice. Run `apm install` first to restore lockfile ownership if you want deployed skills cleaned up.

`--dry-run` keeps the uninstall engine read-only, but `pre-uninstall` lifecycle scripts still run and may have their own side effects. Registry fallback and shared-slot staging are skipped, so marketplace refs not already in the lockfile cannot be previewed and a real shared-slot uninstall can still fail validation; use `owner/repo` notation or re-run without `--dry-run`.

## Related

- [`apm install`](../install/) -- the inverse operation.
- [`apm prune`](../prune/) -- remove orphaned packages without naming them.
- [`apm deps list`](../deps/) -- list installed dependencies and copy portable local identifiers.
- [Lockfile spec](../../lockfile-spec/) -- how `deployed_files` drives safe cleanup.
