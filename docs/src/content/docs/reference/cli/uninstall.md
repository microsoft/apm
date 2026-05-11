---
title: apm uninstall
description: Remove an APM package from the project
sidebar:
  order: 3
---

Remove one or more APM packages from `apm.yml`, the lockfile, `apm_modules/`, and every deployed primitive across all configured harnesses.

## Synopsis

```bash
apm uninstall [OPTIONS] PACKAGES...
```

## Description

`apm uninstall` is the inverse of `apm install <package>`. It strips a package from the manifest, deletes its source from `apm_modules/`, prunes any transitive dependencies that nothing else depends on, and removes every file the package deployed into harness folders (Copilot, Claude, Cursor, Codex, Gemini, OpenCode, Windsurf).

The command only deletes files tracked in the lockfile's `deployed_files` manifest, so hand-authored content in the same harness folders is left alone.

## Arguments

| Argument | Description |
|---|---|
| `PACKAGES...` | One or more packages to remove. Accepts shorthand (`owner/repo`), HTTPS URL, SSH URL, or FQDN. APM resolves each to the canonical identity stored in `apm.yml`. Required. |

## Options

| Option | Description |
|---|---|
| `--dry-run` | Show what would be removed without touching disk. |
| `-v, --verbose` | Show detailed removal information. |
| `-g, --global` | Remove from the user scope (`~/.apm/`) instead of the current project. |

## Examples

Remove one package:

```bash
apm uninstall acme/my-package
```

Remove several at once:

```bash
apm uninstall org/pkg1 org/pkg2
```

Preview the removal without writing to disk:

```bash
apm uninstall acme/my-package --dry-run
```

Remove from the user scope:

```bash
apm uninstall -g acme/my-package
```

Resolve via URL (same identity as the shorthand):

```bash
apm uninstall https://github.com/acme/my-package.git
```

## Behavior

What gets removed, in order:

1. The package entry in `apm.yml` under `dependencies.apm`.
2. The package folder under `apm_modules/owner/repo/`.
3. Transitive dependencies that no remaining package depends on (npm-style pruning, computed from `apm.lock.yaml`).
4. Every file in the lockfile's `deployed_files` for the removed packages and pruned orphans, across all harness folders (`.github/`, `.claude/`, `.cursor/`, `.opencode/`, `.gemini/`, `.codex/`, `.windsurf/`).
5. Hook entries inside `.claude/settings.json`, `.cursor/hooks.json`, and `.gemini/settings.json` that the removed packages contributed.
6. MCP servers contributed only by the removed packages.
7. The lockfile entries themselves. If no dependencies remain, `apm.lock.yaml` is deleted.
8. Empty parent directories left behind by the cleanup.

If a name passed on the command line is not found in `apm.yml`, the command warns and continues with the rest. If none of the names match, it exits without changes.

`--dry-run` runs steps 1-3 in memory and prints the plan; nothing is written.

## Related

- [`apm install`](../install/) -- the inverse operation.
- [`apm prune`](../prune/) -- remove orphaned packages without naming them.
- [`apm list`](../list/) -- see what is currently installed.
- [Lockfile spec](../../lockfile-spec/) -- how `deployed_files` drives safe cleanup.
