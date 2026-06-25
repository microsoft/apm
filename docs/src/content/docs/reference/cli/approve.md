---
title: apm approve / apm deny
description: Manage the executable approval gate for dependency packages.
sidebar:
  order: 25
---

## Synopsis

```bash
apm approve [PACKAGE_REF...] [OPTIONS]
apm deny [PACKAGE_REF...]
```

## Description

APM blocks executable primitives (hooks, bin/ executables, self-defined MCP
servers, and canvas extensions) from dependency packages by default. The
`allowExecutables` block in `apm.yml` opts the **project** in to the gate.
User-specific approvals are stored in **`~/.apm/approvals.yml`** -- a
personal file that is never committed to source control.

`apm approve` adds a package to the user-local allowlist. `apm deny` removes it.

### How the gate works

When `apm install` encounters a dependency that ships executable primitives:

1. If `allowExecutables` is **absent** from `apm.yml`, everything is
   approved (backward-compatible, no gate).
2. If `allowExecutables` is **present** (even empty `{}`), only packages
   approved in `~/.apm/approvals.yml` (or listed directly in `apm.yml`
   for CI pipelines) may deploy executables.
3. In interactive mode, `apm install` prompts for each unapproved
   package. In CI (non-interactive), unapproved executables cause a
   hard error.

Local project content (the root `.apm/` directory) is always trusted.

### What is gated

| Type | Gated | Notes |
|------|-------|-------|
| Hooks (`.apm/hooks/`, `hooks/`) | Yes | Auto-fire in IDE on lifecycle events |
| Bin executables (`bin/`) | Yes | Deployed to agent PATH via symlinks |
| MCP servers (self-defined) | Yes | `registry: false` servers write to IDE MCP config |
| Canvas extensions (`.apm/extensions/`) | Yes | Deploys executable Node.js to IDE extensions |
| Text primitives (skills, agents, instructions) | No | No code execution risk |

### Where approvals are stored

| Store | Path | Who manages it | Committed? |
|-------|------|----------------|------------|
| User-local approvals | `~/.apm/approvals.yml` | `apm approve` / `apm deny` | No |
| Project gate + CI grants | `apm.yml` (`allowExecutables`) | Project maintainer / CI setup | Yes |

Approvals from both stores are merged at install time. The project `apm.yml`
signals that the gate is enabled and may include pre-approved packages for CI;
developer approvals live in the user file and are personal.

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
| `PACKAGE_REF` | One or more packages to deny (removes from user-local allowlist). |

## User approvals file format

`apm approve` writes to `~/.apm/approvals.yml`. The file stores the approvals
mapping directly, keyed by `name#version` with per-type boolean flags:

```yaml
# ~/.apm/approvals.yml  (auto-generated, do not commit)
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
has unapproved executables. Pre-approve packages by listing them
directly in `apm.yml` (this is the only way to share approvals via
source control):

```yaml
# apm.yml
allowExecutables:
  "ci-hooks@acme#1.2.0":
    hooks: true
    bin: true
```

## See also

- [`apm install`](../install/) -- the install command that enforces the gate
- [`apm audit`](../audit/) -- audit installed packages
