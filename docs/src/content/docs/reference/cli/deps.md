---
title: apm deps
description: Inspect, update, and clean installed APM dependencies.
sidebar:
  order: 23
---

Inspect, update, and clean dependencies that `apm install` placed under `apm_modules/`. `apm deps` is a command group; every action lives in a subcommand.

## Synopsis

```bash
apm deps SUBCOMMAND [OPTIONS]
```

## Description

`apm deps` is the read-and-maintenance counterpart to [`apm install`](../install/). It reads `apm.lock.yaml` and the `apm_modules/` tree to show what is installed, refresh git refs, or remove the tree entirely. It does not add new packages -- use `apm install <package>` for that.

All subcommands operate on the project scope (`./apm_modules/`) by default. Pass `-g` / `--global` where supported to operate on the user scope (`~/.apm/apm_modules/`).

## Subcommands

| Subcommand | Purpose |
|---|---|
| `list` | List installed dependencies with per-primitive counts. |
| `tree` | Render the dependency graph as a tree. |
| `info PACKAGE` | Show detailed metadata for one installed package. |
| `update [PACKAGES...]` | Re-resolve git refs and reinstall. |
| `clean` | Remove the entire `apm_modules/` directory. |

### `apm deps list`

List installed dependencies and the primitive counts each one contributes.

```bash
apm deps list [OPTIONS]
```

| Option | Description |
|---|---|
| `-g, --global` | List user-scope dependencies in `~/.apm/` instead of the project. |
| `--all` | Show both project and user-scope dependencies. |
| `--insecure` | Show only dependencies locked to `http://` sources. Adds an `Origin` column distinguishing `direct` declarations from `via <parent>` transitive pulls. |

### `apm deps tree`

Render the dependency graph as a hierarchical tree, using `apm.lock.yaml` when present and falling back to a scan of `apm_modules/`.

```bash
apm deps tree [OPTIONS]
```

| Option | Description |
|---|---|
| `-g, --global` | Show the user-scope tree in `~/.apm/`. |

### `apm deps info`

Show detailed information about one installed package: manifest metadata, primitive inventory, and source. Equivalent to [`apm view PACKAGE`](../view/) for installed packages; prefer `apm view` in new scripts.

```bash
apm deps info PACKAGE
```

| Argument | Description |
|---|---|
| `PACKAGE` | Name of an installed package under `apm_modules/`. Required. |

### `apm deps update`

Re-resolve git references for installed dependencies (direct and transitive), download updated content, re-integrate primitives, and regenerate `apm.lock.yaml`.

```bash
apm deps update [PACKAGES...] [OPTIONS]
```

| Argument | Description |
|---|---|
| `PACKAGES...` | Optional. One or more packages to update. Omit to update everything. |

| Option | Description |
|---|---|
| `-v, --verbose` | Show detailed update information. |
| `--force` | Overwrite locally-authored files on collision. |
| `-t, --target` | Force deployment to specific targets. Comma-separated. Values: `copilot`, `claude`, `cursor`, `opencode`, `codex`, `gemini`, `windsurf`, `agent-skills`, `all`. `agent-skills` deploys to `.agents/skills/` (cross-client). `all` covers every per-client target but excludes `agent-skills`; combine to get both. |
| `--parallel-downloads N` | Max concurrent downloads. Default `4`. `0` disables parallelism. |
| `-g, --global` | Update user-scope dependencies in `~/.apm/`. |
| `--legacy-skill-paths` | Deploy skill files to per-client paths (`.cursor/skills/`, etc.) instead of the shared `.agents/skills/` directory. |

`apm deps update` runs the install pipeline and is gated by org `apm-policy.yml`. There is no `--no-policy` flag; the only escape hatch is `APM_POLICY_DISABLE=1` for the shell session.

### `apm deps clean`

Remove the entire project `apm_modules/` directory. Does not touch `apm.yml` or `apm.lock.yaml`.

```bash
apm deps clean [OPTIONS]
```

| Option | Description |
|---|---|
| `--dry-run` | Show what would be removed without removing. |
| `-y, --yes` | Skip the confirmation prompt (for CI and scripts). |

## Examples

List project dependencies:

```bash
apm deps list
```

Sample output:

```
 Package             Version  Source  Prompts  Instructions  Agents  Skills
 compliance-rules    1.0.0    github  2        1             -       1
 design-guidelines   1.0.0    github  -        1             1       -
```

Show only insecure (HTTP-locked) dependencies and their origin:

```bash
apm deps list --insecure
```

Render the tree:

```bash
apm deps tree
```

```
my-project (local)
+-- compliance-rules@1.0.0
|   +-- 1 instruction, 1 skill
+-- design-guidelines@1.0.0
    +-- 1 instruction, 1 agent
```

Inspect one installed package:

```bash
apm deps info compliance-rules
```

Update everything:

```bash
apm deps update
```

Update specific packages with verbose output:

```bash
apm deps update org/pkg-a org/pkg-b --verbose
```

Preview a clean, then run it non-interactively:

```bash
apm deps clean --dry-run
apm deps clean --yes
```

## Related

- [`apm install`](../install/) -- add packages and run the install pipeline.
- [`apm uninstall`](../uninstall/) -- remove a single package and its deployed files.
- [`apm outdated`](../outdated/) -- check remotes for newer versions without modifying anything.
- [Lockfile spec](../../lockfile-spec/) -- structure of `apm.lock.yaml` that `apm deps` reads.
