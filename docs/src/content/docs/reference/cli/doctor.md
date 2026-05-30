---
title: apm doctor
description: Run environment diagnostics to confirm git, network, auth, and toolchain are healthy.
sidebar:
  order: 15
---

## Synopsis

```bash
apm doctor [OPTIONS]
```

## Description

`apm doctor` runs a bounded set of environment checks and renders a
pass/fail table. It is the first command to reach for when `apm install`
fails, CI behaves differently from a local run, or you want a quick
environment health snapshot.

Checks performed:

| Check | What it verifies |
|---|---|
| git | `git` binary is on PATH and executable. |
| network | `github.com` is reachable via HTTPS. |
| auth | An auth token is available (GitHub PAT or `gh` CLI token). |
| gh CLI | `gh` is installed and authenticated. |
| marketplace config | `marketplace:` block in `apm.yml` is present and valid (only when a marketplace config is detected). |
| format coverage | All declared primitive types have a registered integrator. |
| duplicate names | No two packages declare the same primitive name in the same target scope. |
| version alignment | CLI version matches the lockfile schema version. |

Exit code is `0` when every critical check passes, `1` when any critical
check fails.

## Options

| Flag | Default | Description |
|---|---|---|
| `--verbose`, `-v` | off | Print detail lines below each failing (and passing) check, not just the pass/fail summary row. |

## Examples

```bash
# Quick pass/fail table
apm doctor

# Verbose output with per-check detail
apm doctor --verbose
apm doctor -v
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All critical checks passed. |
| `1` | One or more critical checks failed. The table identifies which. |

## Notes

- `apm marketplace doctor` still works but is deprecated. It prints
  `[!] 'apm marketplace doctor' is deprecated; use 'apm doctor' instead.`
  before delegating to this command. Update any scripts that call the old
  form.

## Related

- [Operating installed context](../../../guides/operating-installed-context/) -- maps common operational questions to the right command.
- [`apm marketplace`](../marketplace/) -- authoring and consumer marketplace commands.
- [`apm audit`](../audit/) -- security scan and lockfile integrity gate.
- [Troubleshooting](../../../troubleshooting/) -- common error patterns and fixes.
