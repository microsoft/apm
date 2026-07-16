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
3. Packages recorded in `apm.lock.yaml` with their `deployed_files`

An installed package is **orphaned** when it is neither declared in either
dependency list nor retained as a lockfile-resolved transitive dependency.
`apm prune` removes the orphan's directory under `apm_modules/`, deletes every
file the orphan deployed into your harness directories (using the
`deployed_files` manifest in the lockfile), removes the entry from
`apm.lock.yaml`, and cleans up empty parent directories.

If `apm_modules/` does not exist, the command exits cleanly with nothing to do. If `apm.yml` is missing, it exits with an error.

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

After processing all orphaned packages, `apm prune` also reconciles merged
hook configuration (`.claude/settings.json`, `.cursor/hooks.json`, and
similar merge targets, plus their `apm-hooks.json` ownership sidecars):
entries owned by a pruned package are removed, while entries owned by
packages that remain declared -- and any manually authored entries -- are
preserved and rewritten back. This orchestrates the same ownership-aware
cleanup `apm uninstall` uses; it does not duplicate the filtering logic.

Notes:

- Packages that share an install root with a still-declared sibling subdirectory dependency are not falsely protected by ancestor expansion. The check uses lockfile membership (with `apm.yml` fallback) to identify genuine standalone packages.
- A manifest embedded at any depth inside an installed package is owned by that
  package. It is not an independent dependency, orphan, or prune candidate.
- Deploy paths are validated before deletion; entries that escape the project root are skipped.
- The command does not network. It only inspects local state.

## Exit codes

| Code | Meaning                                               |
|------|-------------------------------------------------------|
| 0    | Prune completed (including "nothing to prune").       |
| 1    | `apm.yml` missing, parse failure, or unhandled error. |

Per-package removal failures are logged but do not abort the run; remaining orphans still process.

## Related

- [`apm install`](../install/) -- install declared dependencies
- [`apm list`](../list/) -- inspect what is installed
- [Lockfile spec](../../lockfile-spec/) -- `deployed_files` schema
- [Package anatomy](../../../concepts/package-anatomy/) -- what gets deployed where
