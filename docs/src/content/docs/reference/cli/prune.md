---
title: apm prune
description: Remove orphaned packages no longer referenced in apm.yml
sidebar:
  order: 7
---

Remove installed packages from `apm_modules/` that are no longer declared in `apm.yml`, along with their deployed integration files. Updates `apm.lock.yaml` to match.

## Synopsis

```bash
apm prune [--dry-run]
```

:::note[Coming from npm?]
`apm prune` mirrors `npm prune`: it removes installed packages that are not in your manifest. Unlike npm, it also removes the harness-deployed files those packages produced (prompts, agents, hooks, MCP wiring) and rewrites the lockfile.
:::

## Description

`apm prune` reconciles three states:

1. Packages declared in `apm.yml`
2. Packages installed under `apm_modules/`
3. Packages recorded in `apm.lock.yaml` with their `deployed_files`

Anything installed but no longer declared is **orphaned**. `apm prune` removes the orphan's directory under `apm_modules/`, deletes every file the orphan deployed into your harness directories (using the `deployed_files` manifest in the lockfile), removes the entry from `apm.lock.yaml`, and cleans up empty parent directories.

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

Notes:

- Packages that share an install root with a still-declared sibling subdirectory dependency are not falsely protected by ancestor expansion. The check uses lockfile membership (with `apm.yml` fallback) to identify genuine standalone packages.
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
