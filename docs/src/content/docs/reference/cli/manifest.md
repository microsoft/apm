---
title: apm manifest
description: Maintain apm.yml; migrate the deprecated target key to targets
sidebar:
  order: 31
---

Maintain the `apm.yml` manifest.

## Synopsis

```bash
apm manifest migrate [OPTIONS]
```

`migrate` is currently the only `apm manifest` subcommand.

## Description

`apm manifest migrate` rewrites the deprecated top-level `target:` key to
`targets:` (a YAML list). APM warns about `target:` on every run until you
migrate; the key will be removed in v1.0.

The rewrite:

- keeps comments and formatting and changes only the top-level key.
  Dependency `targets:` subsets are never touched;
- deploys exactly the same files and MCP configuration as before;
- changes nothing on a manifest that already uses `targets:` (exit 0).

`apm install` and other commands never rewrite the manifest. Run this
command yourself and commit the result.

## Options

| Flag | Description |
|------|-------------|
| `-g`, `--global` | Migrate the user manifest (`~/.apm/apm.yml`) instead of `./apm.yml`. |
| `--dry-run` | Show the proposed diff without writing. |
| `-y`, `--yes` | Apply without the confirmation prompt. Required in non-interactive shells. |
| `-v`, `--verbose` | Show detailed output. |

## Mapping

| `target:` | `targets:` after migrate |
|---|---|
| `claude,cursor` or `[claude, cursor]` | `[claude, cursor]` |
| `vscode` | `[vscode]` (MCP config stays in `.vscode/mcp.json`) |
| `agents` | `[copilot]` |
| `agy` | `[antigravity]` |
| `all` | Expanded to the explicit list `apm install` deploys today. |
| empty (`target:`) | The key is removed; APM keeps auto-detecting targets. |
| `intellij`, `copilot-app`, other experimental or MCP-only targets | Refused (exit 1). Remove the key, or pass `--target` on the command line. |

After migrating `all`, review the generated list and remove targets you do not use.

## Examples

Preview the change:

```bash
apm manifest migrate --dry-run
```

```text
--- apm.yml (current)
+++ apm.yml (migrated)
@@ -1,4 +1,4 @@
 name: my-project
 version: 1.0.0
-target: claude,vscode  # deploy targets
+targets: [claude, vscode] # deploy targets
```

Apply it without a prompt:

```bash
apm manifest migrate --yes
```

Migrate the user manifest:

```bash
apm manifest migrate --global --yes
```

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Migrated, nothing to migrate, dry run (outside CI), or cancelled at the prompt. |
| `1` | The manifest is missing or invalid, a target has no `targets:` equivalent, `--yes` is missing in a non-interactive shell, or `CI=true` with a change pending. |

## CI

When `CI=true`, `apm manifest migrate` never writes. If the manifest still
uses `target:`, it prints the diff and exits 1, so a pipeline step fails
until the migrated manifest is committed. This includes `--dry-run`. An
up-to-date manifest exits 0.

## Related

- [Manifest schema: `target` and `targets`](../../manifest-schema/#36-target-and-targets)
- [`apm targets`](../targets/)
