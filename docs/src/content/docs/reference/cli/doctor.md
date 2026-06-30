---
title: apm doctor
description: Run local environment diagnostics for git, GitHub reachability, auth, marketplace config, and executable trust.
sidebar:
  order: 11
---

Run a bounded pass/fail diagnostic table for the local APM environment.

## Synopsis

```bash
apm doctor [OPTIONS]
```

## Description

`apm doctor` is the first command to run when an install works on one machine but fails on another, or when CI cannot reproduce a local APM workflow. It checks the core tools and network path APM depends on, then reports a single exit code suitable for scripts.

The command currently covers:

| Check | Type | What it validates |
|---|---|---|
| `git` | critical | `git --version` succeeds and returns before the timeout. |
| `network` | critical | Git can reach `https://github.com/git/git.git` with `ls-remote`. |
| `auth` | informational | A GitHub token is available through APM's auth resolver. |
| `marketplace config` | informational | An `apm.yml` `marketplace:` block or legacy `marketplace.yml`, when present, parses successfully. |
| `format coverage` | informational | Marketplace output formats are configured when marketplace authoring config is present. |
| `duplicate names` | informational | Marketplace package names do not collide. |
| `version alignment` | informational | Local marketplace packages align with the configured versioning strategy. |
| `executable trust` | informational | Local executable approvals are not shadowed by an org policy deny. |

Critical checks determine the exit code. Informational checks explain drift or missing optional setup without failing the command.

## Options

| Flag | Default | Description |
|---|---|---|
| `-v`, `--verbose` | off | Show detailed output for each diagnostic check. |

## Examples

Run the quick table:

```bash
apm doctor
```

Show detailed context:

```bash
apm doctor --verbose
```

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | All critical checks passed. |
| `1` | At least one critical check failed. |

## Related

- [`apm config`](../config/) -- inspect active APM configuration.
- [`apm cache`](../cache/) -- inspect and clean the local cache.
- [`apm runtime`](../runtime/) -- inspect runtime installation state.
- [`apm policy explain`](../policy/) -- inspect executable-trust decisions for a package.
