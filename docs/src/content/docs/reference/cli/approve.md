---
title: apm approve / apm deny
description: Manage the executable approval gate for dependency packages.
sidebar:
  order: 25
---

## Synopsis

```bash
apm approve [PACKAGE_REF...] [OPTIONS]
apm deny [PACKAGE_REF...] [OPTIONS]
```

## Description

APM blocks executable primitives (hooks, bin/ executables) from
dependency packages by default. The `allowExecutables` block in
`apm.yml` records which packages have been explicitly approved to
deploy executables.

`apm approve` adds a package to the allowlist. `apm deny` removes it.

### How the gate works

When `apm install` encounters a dependency that ships hooks or bin/
executables:

1. If `allowExecutables` is **absent** from `apm.yml`, everything is
   approved (backward-compatible, no gate).
2. If `allowExecutables` is **present** (even empty `{}`), only listed
   packages may deploy executables.
3. In interactive mode, `apm install` prompts for each unapproved
   package. In CI (non-interactive), unapproved executables cause a
   hard error.

Local project content (the root `.apm/` directory) is always trusted.

### What is gated

| Type | Gated | Notes |
|------|-------|-------|
| Hooks (`.apm/hooks/`, `hooks/`) | Yes | Auto-fire in IDE on lifecycle events |
| Bin executables (`bin/`) | Yes | Deployed to agent PATH via symlinks |
| MCP servers | No | Enforcement deferred to a future release |
| Text primitives (skills, agents, instructions) | No | No code execution risk |

## Options

### `apm approve`

| Flag | Description |
|------|-------------|
| `PACKAGE_REF` | One or more packages to approve (e.g. `ci-hooks@acme`). |
| `--pending` | List all packages with unapproved executables. |
| `--all` | Approve all currently blocked packages. |

### `apm deny`

| Flag | Description |
|------|-------------|
| `PACKAGE_REF` | One or more packages to deny (removes from allowlist). |

## Manifest format

Approvals are stored in `apm.yml` under `allowExecutables`, keyed by
`name#version` with per-type boolean flags:

```yaml
allowExecutables:
  "ci-hooks@acme#1.2.0":
    hooks: true
    bin: true
  "dev-tools@org#0.5.0":
    hooks: true
```

Version pinning means approval must be renewed when a package updates.

## Examples

Approve a specific package:

```bash
apm approve ci-hooks@acme
```

Show all blocked packages:

```bash
apm approve --pending
```

Approve everything (migration helper):

```bash
apm approve --all
```

Revoke approval:

```bash
apm deny ci-hooks@acme
```

## Non-interactive / CI usage

In CI environments (`CI=true`, `APM_NON_INTERACTIVE=1`, or when stdin
is not a TTY), `apm install` fails with exit code 1 if any dependency
has unapproved executables. Pre-approve packages in `apm.yml` before
CI runs:

```bash
# One-time setup: approve all current dependencies
apm approve --all
git add apm.yml
git commit -m "Approve executable dependencies"
```

## See also

- [`apm install`](../install/) -- the install command that enforces the gate
- [`apm audit`](../audit/) -- audit installed packages
