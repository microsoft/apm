---
title: apm find
description: Trace a deployed file back to the package(s) that installed it.
sidebar:
  order: 28
---

Reverse-lookup: given a file path on disk, `apm find` reports which package(s) in `apm.lock.yaml` deployed it.

## Synopsis

```bash
apm find <PATH> [OPTIONS]
```

`PATH` is the path to the deployed file you want to trace (relative path from the project root).

## Description

`apm find` reads `apm.lock.yaml`, builds a reverse index from every package's `deployed_files` list, and prints the name of every package that claims the file. It is the inverse of `apm install`: instead of asking "what does this package deploy?", you are asking "what package deployed this file?"

The command is **read-only**. It performs zero network requests, zero auth calls, and zero file writes. It never modifies `apm.lock.yaml` or any deployed file.

When multiple packages deployed the same file (common for shared harness files such as `AGENTS.md` or `CLAUDE.md` written by several contributors), `apm find` lists all of them, one per line.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--source` | off | After each package name, print the OCI image URI, git remote URL, or local path that is the origin of that package on the same line. |
| `--path` | off | After each package name, print the full dependency chain from that package up to the root (same output as `apm deps why`). |

## Examples

### Basic lookup

```bash
apm find .github/copilot-instructions.md
```

Output (file found in one package):

```
owner/repo
```

### Show origin source

```bash
apm find .github/copilot-instructions.md --source
```

Output:

```
owner/repo  https://github.com/owner/repo.git@abc1234
```

### Show full dependency chain

```bash
apm find .github/copilot-instructions.md --path
```

Output:

```
owner/repo
  apm.yml -> owner/repo
```

### Multi-contributor file (AGENTS.md / CLAUDE.md)

Shared harness files can be contributed by more than one package. All contributors are listed:

```bash
apm find AGENTS.md
```

Output:

```
owner/repo-a
owner/repo-b
```

Combine with `--source` to see where each contributor came from:

```bash
apm find AGENTS.md --source
```

Output:

```
owner/repo-a  https://github.com/owner/repo-a.git@def5678
owner/repo-b  oci://ghcr.io/owner/repo-b:v1.2.0
```

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | File found in at least one package's `deployed_files`. |
| `1` | File not found in any package's `deployed_files`. |
| `2` | Lockfile is missing or cannot be read. |

Error messages are written to stderr with a `[x]` prefix. Package names are written to stdout, one per line.

## Related

- [`apm deps why`](../deps/#apm-deps-why) -- explain why a package is installed (the `--path` output uses the same walker).
- [`apm install`](../install/) -- installs packages and writes `apm.lock.yaml`.
- [Lockfile spec](../../lockfile-spec/) -- the `deployed_files` field that `apm find` reads.
