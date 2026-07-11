---
title: apm export-patch
description: Export local edits to APM-managed files as patches against their source packages.
sidebar:
  order: 14
---

## Synopsis

```bash
apm export-patch [OPTIONS]
```

## Description

`apm export-patch` turns local edits to APM-managed files into unified diffs against the packages that deployed them, so the edits can be contributed back upstream.

It replays the locked install into a scratch tree (the same machinery as the [`apm audit`](../audit/) drift check) and, for every managed file that was modified locally, maps the change back to the package source file that produced it. Each package with exportable edits gets one `.patch` file, ready for `git apply` in a clone of the package repository.

Every patch file records its base in a leading comment block: the package key, its source, and the exact snapshot the diff applies to (`commit <sha>` for git dependencies, `version <x>` for registry packages). Apply the patch from the package repository root, checked out at that base, then open your upstream pull request from the result:

```bash
git -C path/to/package-clone checkout <base-commit>
git -C path/to/package-clone apply path/to/apm-patches/<package>.patch
```

Only verbatim-deployed files can be exported: a local edit maps cleanly back to its source only when the deployment copied that source byte-for-byte. Deployments that transform their content — frontmatter rewrites for rule directories, compiled `AGENTS.md` output, aggregated `copilot-instructions.md` sections, files with resolved links — are listed as skipped with the reason, and the change must be ported to the package source by hand. Local deletions of managed files are never exported, since deleting a deployed copy does not imply the file should be removed from the package.

Findings that belong to local path dependencies or to the project's own `.apm/` content are also skipped: their source already lives on disk, so the edit belongs there directly.

The replay is cache-only and read-only: it needs a lockfile and a populated `apm_modules/` (run `apm install` first) and never mutates the project tree. Patch files are the only output.

## Options

| Flag | Default | Description |
|---|---|---|
| `--out`, `-o DIR` | `apm-patches` | Directory to write per-package `.patch` files into. Created if missing; only created when there is something to export. |
| `--dry-run` | off | List what would be exported (and what gets skipped, with reasons) without writing patch files. |
| `--verbose`, `-v` | off | Show replay progress and per-file skip details. |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, including "nothing to export". |
| 1 | Replay or export failed (missing lockfile, cold cache, unreadable files). |

## Examples

```bash
# Export all local edits as per-package patches under ./apm-patches/
apm export-patch

# Preview what would be exported without writing anything
apm export-patch --dry-run

# Write patches somewhere else
apm export-patch -o /tmp/spec-patches
```

## See also

- [`apm audit`](../audit/) — detect the drift this command exports.
- [Drift and secure by default](../../../consumer/drift-and-secure-by-default/) — why local edits to managed files are overwritten on the next install.
