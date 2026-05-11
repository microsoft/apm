---
title: apm cache
description: Inspect and manage the local APM package cache
sidebar:
  order: 9
---

Inspect and maintain the local cache APM uses to avoid redundant
network I/O during `apm install`.

## Synopsis

```bash
apm cache info
apm cache clean [--force | --yes]
apm cache prune [--days N]
```

## Description

`apm cache` groups three subcommands that operate on the local cache
root. The cache holds two independent stores:

- **Git cache** -- bare repository databases plus per-SHA worktree
  checkouts, keyed by resolved commit.
- **HTTP cache** -- conditional-GET responses for the GitHub release
  and API endpoints APM polls during install.

The cache is purely a performance optimization. Removing it never
breaks correctness; the next `apm install` re-fetches whatever it
needs.

## Subcommands

### `apm cache info`

Show the resolved cache root, per-store entry counts, and a size
breakdown.

```bash
apm cache info
```

Output:

```
[i] Cache root: /Users/you/Library/Caches/apm
  Git repositories (db):    12
  Git checkouts:            34
  HTTP cache entries:       87

  Total size:               142.3 MB
    Git:                    138.1 MB
    HTTP:                   4.2 MB
```

### `apm cache clean`

Remove every entry from both the git and HTTP caches. Prompts for
confirmation unless a skip flag is passed.

```bash
apm cache clean              # interactive prompt
apm cache clean --force      # non-interactive
apm cache clean --yes        # alias for --force
```

| Flag | Description |
|---|---|
| `--force`, `-f` | Skip the confirmation prompt. |
| `--yes`, `-y` | Alias for `--force`. Use in CI scripts so the command never blocks on stdin. |

:::caution
`clean` removes every cached commit and every cached HTTP response.
The next `apm install` will re-fetch everything from the network.
Use `prune` when you only want to reclaim space from stale entries.
:::

### `apm cache prune`

Remove git-cache checkouts whose filesystem `mtime` is older than
`--days N`. Defaults to 30 days. The HTTP cache is not touched.

```bash
apm cache prune              # default: older than 30 days
apm cache prune --days 7     # tighter window
```

| Flag | Description |
|---|---|
| `--days N` | Remove entries not accessed within this many days. Default: `30`. |

:::caution[Lockfile-blind]
`prune` does not consult any project's `apm.lock.yaml` before
evicting entries. A pinned commit SHA referenced by your lockfile may
be pruned if nothing has touched its checkout recently; the next
`apm install` will re-clone it from the remote. Safe, but can
surprise air-gapped or rate-limited environments.
:::

## Cache layout

The cache root resolves in this precedence order (first match wins):

1. `APM_NO_CACHE=1` -- per-invocation temp directory, cleaned at exit.
2. `APM_CACHE_DIR=/path` -- explicit override.
3. Platform default:
   - **macOS:** `~/Library/Caches/apm/`
   - **Linux:** `${XDG_CACHE_HOME:-~/.cache}/apm/`
   - **Windows:** `%LOCALAPPDATA%\apm\Cache\`

Inside the cache root:

```
<cache-root>/
  git/
    db_v1/           # bare repository databases
    checkouts_v1/    # per-SHA worktree checkouts
  http_v1/           # conditional-GET response cache
```

The cache root is created with mode `0700` and validated to be
absolute with no NUL bytes before use.

## Environment variables

| Variable | Effect |
|---|---|
| `APM_CACHE_DIR` | Override the cache root. Must be an absolute path. |
| `APM_NO_CACHE` | When set to `1`, `true`, or `yes`, route all cache I/O to a temp directory cleaned at process exit. |
| `XDG_CACHE_HOME` | Honored on Linux and (when explicitly set) macOS. |

## Coming from npm?

`apm cache clean` mirrors `npm cache clean`: it nukes the local cache
and forces re-download on next install. There is no `--dry-run` and
no per-package targeting; cleaning is all-or-nothing.

## Related

- [`apm install`](../install/) -- populates the cache during dependency resolution.
- [Lockfile spec](../../lockfile-spec/) -- what gets pinned and re-fetched.
