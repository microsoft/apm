---
title: apm doctor
description: Diagnose git, network, authentication, marketplace, and executable-trust configuration.
sidebar:
  order: 29
---

## Synopsis

```bash
apm doctor [--verbose]
```

## Description

`apm doctor` runs environment diagnostics and prints a pass/fail table. Use it
during onboarding or as a pre-flight check when installs and marketplace
operations fail unexpectedly.

The network probe runs `git ls-remote` against GitHub and can take up to five
seconds before timing out.

## Checks

| Check | What it verifies | Affects exit code? |
|---|---|---|
| Git | `git --version` succeeds. | Yes |
| Network | Git can read the `HEAD` ref from `github.com/git/git.git`. | Yes |
| Authentication | APM's credential resolver finds a token for `github.com`. The resolver can use environment variables, the GitHub CLI, or a git credential helper. A missing token means unauthenticated rate limits apply. | No |
| Marketplace config | The `marketplace:` block in `apm.yml`, or legacy `marketplace.yml`, can be parsed when present. | No |
| Marketplace authoring | Configured output formats, duplicate package names, and version alignment are reported when marketplace config is present. | No |
| Executable trust | In an APM project, reports local allows overridden by organization policy and points to `apm policy explain`. | No |

The GitHub CLI is a possible credential source; `apm doctor` does not require
it or report its installation as a separate check.

## Options

| Flag | Description |
|---|---|
| `-v`, `--verbose` | Show detailed diagnostic logging. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The Git and network checks passed. |
| `1` | Git is unavailable or the network probe failed. |

Informational failures do not change the exit code.

## Examples

Run the standard diagnostics:

```bash
apm doctor
```

Include detailed output:

```bash
apm doctor --verbose
```

Use the exit code as a basic CI pre-flight:

```bash
apm doctor && apm audit --ci
```

For project integrity and policy enforcement in CI, use
[`apm audit --ci`](../audit/) instead.

## Related

- [`apm audit`](../audit/) -- validate lockfile, deployed content, and policy
  in CI.
- [`apm policy`](../policy/) -- inspect organization policy and executable
  trust decisions.
- [Authentication](../../../getting-started/authentication/) -- configure
  credentials used by APM.
