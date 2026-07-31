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
| `PACKAGES...` | One or more packages to remove. Accepts shorthand (`owner/repo`), HTTPS URL, SSH URL, FQDN, marketplace notation (`name@marketplace`), an exact declared local path, or the portable `_local/<name>` identifier printed for a direct local dependency with matching lock metadata. Required. |

## Options

| Option | Description |
|---|---|
| `--dry-run` | Show the removal plan without writes from the uninstall engine. `pre-uninstall` lifecycle scripts still run and may have side effects. Registry fallback and shared-slot survivor staging are skipped. |
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

Preview the uninstall engine's plan (pre-uninstall scripts still run):

```bash
apm uninstall acme/my-package --dry-run
```

Remove from the user scope:

```bash
apm uninstall -g acme/my-package
```

Remove a local dependency by copying the portable key from `apm deps list`:

```bash
apm deps list
apm uninstall "_local/my-local-package"
```

The same key works at user scope, so scripts do not need the absolute path used
when the local package was installed:

```bash
apm deps list -g
apm uninstall -g "_local/my-local-package"
```

Remove by marketplace name (resolved via lockfile, then registry):

```bash
apm uninstall my-plugin@official
```

Resolve via URL (same identity as the shorthand):

```bash
apm uninstall https://github.com/acme/my-package.git
```

## Behavior

What gets removed, in order:

1. The package entry in `apm.yml` under `dependencies.apm` or `devDependencies.apm`.
2. The package folder under `apm_modules/owner/repo/`.
3. Transitive dependencies that no remaining package depends on (npm-style pruning, computed from `apm.lock.yaml`). A transitive dependency still declared by any surviving package is preserved, even when two packages share it (a diamond-shaped install). If a surviving package's manifest can't be read, APM keeps every remaining candidate for that run rather than guessing -- re-run with `--verbose` to see which manifest failed, then fix or restore it and re-run to complete cleanup.
4. Every file in the lockfile's `deployed_files` for the removed packages and pruned orphans, across all harness folders (`.github/`, `.claude/`, `.cursor/`, `.opencode/`, `.gemini/`, `.codex/`, `.windsurf/`, `.kiro/`).
5. Hook entries inside `.claude/settings.json`, `.cursor/hooks.json`, `.gemini/settings.json`, and `.kiro/hooks/` that the removed packages contributed. Remaining packages -- including transitive dependencies still required by another package -- have their hook entries rebuilt from the post-removal lockfile.
6. MCP servers contributed only by the removed packages.
7. The lockfile entries themselves. If no dependencies remain, `apm.lock.yaml` is deleted.
8. Empty parent directories left behind by the cleanup.

Selection is atomic. If any requested identifier does not match a declaration,
the command exits nonzero before lifecycle scripts or filesystem writes run. No
matched package in the same invocation is removed. Fix the identifier and retry.

`_local/<name>` is resolved from the manifest and lockfile metadata that produced
the `apm deps list` row. APM does not reinterpret it as `owner/repo` or rebuild an
absolute path. If two declared local dependencies have the same portable key,
selection is ambiguous and exits nonzero without changes. Use one exact path
already declared in the relevant manifest (`apm.yml` or `~/.apm/apm.yml`); APM
never removes both or guesses, and diagnostics do not print declared paths.
When an exact path removes one declaration from a shared local install slot, APM
validates and stages the survivor before changing the manifest, then activates
it during cleanup.
If more than one declaration would remain in that slot, uninstall fails without
APM writes. Pass enough exact paths in one command that at most one declaration
remains.

If a marketplace ref cannot be resolved (neither the lockfile nor the registry has a matching entry), APM logs an error and aborts without changes. Use `owner/repo` notation to uninstall directly, or run `apm deps list` to find the canonical name.

### Supply-chain guard

When marketplace notation (`name@marketplace`) falls through to the registry (Stage 2), APM refuses any canonical the registry returns that is not already recorded in `apm.lock.yaml`. The refusal is reported as a warning naming the resolved canonical so you can decide whether to re-run with `apm uninstall owner/repo` directly. This prevents a poisoned marketplace registry from coercing APM into removing an unrelated installed package.

### `#ref` is not meaningful for `uninstall`

`apm install` accepts an optional `#ref` fragment (`apm install NAME@MKT#ref`) to pin a specific revision. `apm uninstall` identifies packages by canonical name only, so any `#ref` fragment supplied with marketplace notation (e.g. `my-plugin@official#v1.0.0`) is ignored.

### No-lockfile behavior

If `apm.lock.yaml` is not present, marketplace notation has no offline anchor: Stage 1 finds nothing, and the supply-chain guard cannot cross-check the registry result. APM still attempts registry resolution and proceeds if the canonical matches an entry in `apm.yml`, but this path has weaker integrity guarantees. A local `_local/<name>` key also has no persisted mapping without the lockfile, so use its exact declared path or run `apm install` to regenerate the lockfile first.

`--dry-run` keeps the uninstall engine read-only, but `pre-uninstall` lifecycle scripts still run and may have their own side effects. Registry fallback and shared-slot staging are skipped, so marketplace refs not already in the lockfile cannot be previewed and a real shared-slot uninstall can still fail validation; use `owner/repo` notation or re-run without `--dry-run`.

## Related

- [`apm install`](../install/) -- the inverse operation.
- [`apm prune`](../prune/) -- remove orphaned packages without naming them.
- [`apm deps list`](../deps/) -- list installed dependencies and copy portable local identifiers.
- [Lockfile spec](../../lockfile-spec/) -- how `deployed_files` drives safe cleanup.
