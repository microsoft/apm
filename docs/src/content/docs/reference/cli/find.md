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

When multiple packages deployed the same file, `apm find` lists all of them, one per line. Compiled root files (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) are written by `apm compile`, not recorded as deployments, so `apm find` does not trace them; trace the instruction files they include.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--source` | off | After each package name, print the origin of that package on the same line: the OCI image URI, the local path, or the package with its locked ref, tag, or short commit (`owner/repo@ref`). |
| `--path` | off | After each package name, print the full dependency chain from `apm.yml` down to that package (same walker as `apm deps why`). |

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
owner/repo  owner/repo@v1.0.0
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

### File deployed by several packages

Every package that recorded the path is listed:

```bash
apm find .agents/skills/shared/SKILL.md
```

Output:

```
owner/repo-a
owner/repo-b
```

Combine with `--source` to see where each one came from:

```bash
apm find .agents/skills/shared/SKILL.md --source
```

Output:

```
owner/repo-a  owner/repo-a@main
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
- [`apm view --file`](../view/#apm-view---file-path) -- exact-path owners from deployment records, with active writer, ref, commit and current file status.
- [`apm install`](../install/) -- installs packages and writes `apm.lock.yaml`.
- [Lockfile spec](../../lockfile-spec/) -- the `deployed_files` field that `apm find` reads.
