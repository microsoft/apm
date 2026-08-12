---
title: apm enroll
description: Onboard a machine onto a marketplace -- verify credentials, register, and confirm it is browsable.
sidebar:
  order: 30
---

## Synopsis

```bash
apm enroll SOURCE [--name ALIAS] [--ref REF] [--host FQDN] [--skip-verify] [--verbose]
```

## Description

`apm enroll` performs the three steps a new joiner otherwise runs by hand:

1. **Verify credentials** -- confirm a usable token exists for the marketplace
   host, prompting for one if not.
2. **Register** the marketplace (equivalent to [`apm marketplace add`](../marketplace/)).
3. **Smoke-test** it by browsing the plugin list.

It is safe to re-run. When a working credential is already configured, the
token step is skipped entirely.

Only the credential step is new behaviour; registration and browsing delegate
to `apm marketplace add` and `apm marketplace browse`, so `SOURCE` accepts
exactly the same forms.

## Credentials

APM resolves GitLab tokens in this order: `GITLAB_APM_PAT`, then
`GITLAB_TOKEN`, then your git credential helper. Because the environment
variables are consulted first, exporting one is sufficient -- no git
configuration change is required.

The credential check calls the GitLab REST API rather than testing `git`
access. This distinction matters: an OAuth session token is valid for
`git clone` but returns `401` from the REST API, which is what marketplace
lookups use. A token needs the scopes `read_repository,read_api`.

When no usable token is found in an interactive terminal, `apm enroll` opens
GitLab's token-creation page with the name and scopes prefilled, then prompts
for the result. A pasted token is verified before use and applies to the
current command only -- export it to persist it:

```bash
export GITLAB_APM_PAT='glpat-...'
```

The credential pre-check is GitLab-specific. Other hosts go straight to
registration, using whatever credentials `apm marketplace add` already
resolves.

:::note[Shadowing credential helpers]
On macOS, the system `osxkeychain` helper is unscoped: a stale entry for a
host answers `git credential fill` before any scoped helper and keeps winning.
`apm enroll` detects this and prints the command to clear it. It never erases
credentials for you.
:::

## Arguments

| Argument | Description |
|---|---|
| `SOURCE` | The marketplace to enroll on. Accepts `OWNER/REPO` or `HOST/OWNER/REPO` shorthand, a full HTTPS or SSH git URL, a local path, or a `file://` URI. |

## Options

| Flag | Description |
|---|---|
| `-n`, `--name` | Marketplace alias. Defaults to the repository name. |
| `-r`, `--ref` | Git ref (branch, tag, or commit). Default: `main`. |
| `--host` | Git host FQDN for `OWNER/REPO` shorthand. Default: `github.com`. |
| `--skip-verify` | Skip the credential pre-check and go straight to registration. |
| `-v`, `--verbose` | Show detailed output. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The marketplace was registered and is browsable. |
| `1` | The source was invalid, no usable credential was available, or registration or browsing failed. |

## Non-interactive use

`apm enroll` never prompts when `APM_NON_INTERACTIVE` or `CI` is set, or when
stdin is not a TTY. Without a usable credential it exits `1` and names the
environment variable to set, rather than hanging.

```bash
export GITLAB_APM_PAT='glpat-...'
apm enroll gitlab.com/acme/team/apm-marketplace --name acme
```

## Examples

Enroll on a private GitLab marketplace:

```bash
apm enroll gitlab.com/acme/team/apm-marketplace --name acme
```

Use a full URL and pin a ref:

```bash
apm enroll https://gitlab.com/acme/apm-marketplace --name acme --ref v1.2.0
```

Register a local marketplace for development:

```bash
apm enroll ./local-marketplace --name scratch
```

After enrolling, install a plugin:

```bash
apm install <plugin-name>@acme --target claude
```

## Related

- [`apm marketplace`](../marketplace/) -- manage registered marketplaces
  individually.
- [`apm doctor`](../doctor/) -- diagnose git, network, and authentication
  problems.
- [Authentication](../../../getting-started/authentication/) -- configure
  credentials used by APM.
