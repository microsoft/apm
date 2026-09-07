---
title: apm cache
description: Inspect and manage the local APM package cache
sidebar:
  order: 9
---

Inspect and maintain the local cache APM uses to avoid redundant
network I/O during dependency installs and MCP registry lookups.

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
- **HTTP cache** -- conditional-GET responses for MCP registry
  endpoints.

A fresh, integrity-verified HTTP cache hit updates only the entry
directory's `mtime`. This recency marker drives LRU eviction; it does
not rewrite stored metadata or extend the response TTL. If the
`mtime` update fails, APM logs the failure at debug level and returns
the verified cached response. Stores and successful 304 refreshes
also update the directory `mtime`.

The cache is purely a performance optimization. Removing it never
breaks correctness; the next dependency install or MCP registry
lookup re-fetches whatever it needs.

Plain and frozen installs can reuse locked SHAs when upstream is unavailable.
Commands that require current state -- `apm install --update`, `apm install
--refresh`, `apm update` (including `--force`), `apm lock --update`, and `apm
outdated` -- resolve upstream first. Update may reuse content for the resolved
SHA; `--refresh` bypasses it.

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
| `--force`, `-f` | Skip the confirmation prompt. Does not suppress deletion failures or make the command succeed. |
| `--yes`, `-y` | Alias for `--force`. Use in CI scripts so the command never blocks on stdin. |

If an entry can't be deleted -- a locked file, a permissions error --
`clean` still removes every other entry, then reports the incomplete
cleanup with the affected paths and exits non-zero. Successful
removals are not rolled back. Close the process holding the lock or
fix permissions, then retry.

:::caution
`clean` removes every cached commit and every cached HTTP response.
The next dependency install or MCP registry lookup will re-fetch the
required data from the network.
Use `prune` when you only want to reclaim space from stale entries.
:::

### `apm cache prune`

Remove Git-cache SHA groups whose shared `mtime` is older than `--days N`.
Reusing a full or sparse variant refreshes the group timestamp; pruning removes
all variants. The default is 30 days. The HTTP cache is not touched.

```bash
apm cache prune              # default: older than 30 days
apm cache prune --days 7     # tighter window
```

Output counts SHA groups, not checkout variants:

```text
Pruning SHA groups older than 30 days...
Pruned 2 SHA group(s).
```

| Flag | Description |
|---|---|
| `--days N` | Remove SHA groups not accessed within this many days. Default: `30`. |

A recency-only permission error after successful checkout validation is
non-fatal:

```text
[!] Cannot update Git cache recency for <sha-root>: <cause>. Continuing with validated checkout; cache prune may evict it. Check cache permissions or set APM_CACHE_DIR to a writable directory.
```

Other filesystem errors and validation failures remain fatal.

:::note
`--days` accepts a nonnegative integer. Negative values are rejected
before the cache is touched. `0` makes every past entry eligible for
removal.
:::

:::caution[Lockfile-blind]
`prune` does not consult project lockfiles. It can evict every variant for a
locked SHA. If the bare repository cannot rebuild the checkout, the next
install requires remote access. Freshness-required commands resolve upstream
regardless of retained cache entries.
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
                     #   <shard>/      -- full bare clone (default)
                     #   <shard>__p/   -- partial bare clone
                     #                    (--filter=blob:none) used
                     #                    for sparse-checkout consumers
    checkouts_v1/    # per-SHA worktree checkouts, variant-keyed
                     #   <shard>/<sha>/full/             -- full tree
                     #   <shard>/<sha>/sparse-<hash>/    -- sparse cone, or a
                     #                                     full tree when a
                     #                                     symlink target lies
                     #                                     outside the cone
                     #                                     (<hash> = first
                     #                                      16 hex of
                     #                                      sha256(paths))
  http_v1/           # conditional-GET response cache
```

The `full/` and `sparse-<variant>/` subdirs let two consumers of the
same commit share storage when they want the same subdirs, and keep
distinct shards when they do not -- without the variant suffix the
sparse checkout would clobber the full tree for any other consumer
of that SHA. A sparse variant widens to the full tree when a package
symlink targets a tracked file excluded from the sparse cone, so that
variant can consume more disk than its name suggests.

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
and forces dependencies and registry responses to be downloaded again
when next needed. There is no `--dry-run` and no per-package targeting;
cleaning is all-or-nothing.

## Related

- [`apm install`](../install/) -- populates the cache during dependency resolution.
- [`apm mcp`](../mcp/) -- resolves MCP servers through registry lookups.
- [Lockfile spec](../../lockfile-spec/) -- what gets pinned and re-fetched.
