---
title: apm prune
description: Remove packages absent from the resolved dependency graph
sidebar:
  order: 7
---

Remove installed packages from `apm_modules/` that are neither declared in
`apm.yml` nor retained as transitive nodes in `apm.lock.yaml`. The command also
removes their deployed integration files and updates the lockfile.

## Synopsis

```bash
apm prune [--dry-run]
```

:::note[Coming from npm?]
`apm prune` mirrors `npm prune`: it removes installed packages that are not in
the manifest's resolved dependency graph. Unlike npm, it also removes the
harness-deployed files those packages produced (prompts, agents, hooks, MCP
wiring) and rewrites the lockfile.
:::

## Description

`apm prune` reconciles three states:

1. Packages declared in `apm.yml` (both `dependencies.apm` and `devDependencies.apm`)
2. Packages installed under `apm_modules/`
3. Packages recorded in `apm.lock.yaml`, including their `deployed_files` and
   canonical deployment ownership rows

An installed package is **orphaned** when it is neither declared in either
dependency list nor retained as a lockfile-resolved transitive dependency.
`apm prune` removes the orphan's directory under `apm_modules/`, deletes every
file the orphan deployed into your harness directories (using the
`deployed_files` manifest in the lockfile), removes the entry from
`apm.lock.yaml`, and cleans up empty parent directories.

`apm prune` also parses and reconciles the lockfile's canonical deployment
ownership metadata on every run, even when `apm_modules/` does not exist or
no package is orphaned. It also removes stale direct-dependency records whose
package directory is already absent, allowing a retry to finish after an
earlier lockfile write failed. A stale dependency or owner reference is not
"nothing to prune." If `apm.yml` is missing, the command exits with an error.

## Options

| Option      | Description                                       |
|-------------|---------------------------------------------------|
| `--dry-run` | List orphaned packages without removing anything. |

## Examples

Remove orphaned packages:

```bash
apm prune
```

Preview what would be removed:

```bash
apm prune --dry-run
```

Typical workflow after editing `apm.yml`:

```bash
# Remove a dependency from apm.yml, then:
apm install   # installs the new state
apm prune     # cleans up what is no longer declared
```

## Behavior

For each orphaned package, `apm prune`:

1. Removes the package directory from `apm_modules/<owner>/<repo>` using a path-traversal-safe delete.
2. Reads `deployed_files` from the lockfile entry and deletes each deployed file or directory inside the project root.
3. Removes the entry from `apm.lock.yaml`.
4. Cleans up empty parent directories under both `apm_modules/` and the harness deploy roots.
5. Deletes `apm.lock.yaml` if pruning leaves it with zero dependencies.

### Canonical deployment ownership

`apm prune` also reconciles the canonical `deployments` rows in
`apm.lock.yaml` (see [Lockfile spec](../../lockfile-spec/#canonical-deployment-rows)):

- **Trusted deletion authority.** Only a pruned dependency's own
  pre-transition `deployed_files` and `deployed_file_hashes` -- captured
  before that dependency's entry is removed this run -- authorize deleting
  its bytes. Paths still claimed by a surviving owner after reconciliation
  are subtracted from that claim first and are never deleted, including
  external URI deployments such as `cowork://` skills.
- **Ghost rows are metadata-only repair.** A canonical row referencing an
  owner that no longer exists in the lockfile is repaired in the ledger, but
  that repair never authorizes deleting the file the row points at. Existing
  bytes that no trusted claim covers are preserved; `apm prune` logs a
  warning naming the path and asking you to inspect and remove it manually.
- **User-edited, failed, or unmanaged files** follow the same rule: prune
  removes the stale ownership record, never the untrusted bytes.
- **`--dry-run`** previews both the packages that would be removed and the
  ownership records that would be repaired, without mutating anything.
- Rerunning `apm prune` after a partial or interrupted run converges to the
  same clean state (idempotent), even when package deletion succeeded before
  the prior run failed to write `apm.lock.yaml`.

After processing all orphaned packages, `apm prune` also reconciles merged
hook configuration (`.claude/settings.json`, `.cursor/hooks.json`, and
similar merge targets, plus their `apm-hooks.json` ownership sidecars):
entries owned by a pruned package are removed, while entries owned by
packages that remain in the post-prune lockfile -- direct *and*
transitive -- and any manually authored entries are preserved and
rewritten back. This orchestrates the same ownership-aware cleanup
`apm uninstall` uses; it does not duplicate the filtering logic.
Hook reconciliation is best-effort: a failure is logged as a warning but
does not abort the run, since package and lockfile cleanup has already
completed by that point. If reconciliation logs a warning, run `apm
install` to rebuild hook configuration from the current dependency set.
To clean up hooks left by a target removed from `targets:` in `apm.yml`,
run `apm install` (or `apm compile` / `apm update`); `apm prune` only
reconciles hooks for packages and targets still declared.

Notes:

- Packages that share an install root with a still-declared sibling subdirectory dependency are not falsely protected by ancestor expansion. The check uses lockfile membership (with `apm.yml` fallback) to identify genuine standalone packages.
- A manifest embedded at any depth inside an installed package is owned by that
  package. It is not an independent dependency, orphan, or prune candidate.
- Deploy paths are validated before deletion; entries that escape the project root are skipped.
- The command does not network. It only inspects local state.

## Exit codes

| Code | Meaning                                                                       |
|------|--------------------------------------------------------------------------------|
| 0    | Prune completed (including "nothing to prune").                              |
| 1    | `apm.yml` missing, parse failure, lockfile write failure, or unhandled error. |

Per-package removal failures are logged but do not abort the run; remaining
orphans still process. A lockfile write failure after filesystem cleanup has
already started exits `1` with a partial-cleanup warning: rerun `apm prune`,
then run `apm audit` to confirm the resulting state.

## Related

- [`apm install`](../install/) -- install declared dependencies
- [`apm uninstall`](../uninstall/) -- remove a declared dependency (shares this command's hook-reconciliation owner)
- [`apm list`](../list/) -- inspect what is installed
- [Lockfile spec](../../lockfile-spec/) -- `deployed_files` schema
- [Package anatomy](../../../concepts/package-anatomy/) -- what gets deployed where
