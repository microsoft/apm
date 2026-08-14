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

APM resolves tokens itself, and the environment variables are consulted
before any git credential helper -- so exporting one is sufficient and no git
configuration change is required.

| Host | Resolution order | Scopes |
|---|---|---|
| GitLab | `GITLAB_APM_PAT`, `GITLAB_TOKEN`, git credential helper | `read_repository,read_api` |
| GitHub / GHES | `GITHUB_APM_PAT`, `GITHUB_TOKEN`, `gh auth token`, git credential helper | `repo` |

The credential check calls the host's REST API rather than testing `git`
access. This distinction matters: on GitLab, an OAuth session token is valid
for `git clone` but returns `401` from the REST API, which is what marketplace
lookups use.

The probe tries every location a manifest can live in (`marketplace.json`,
`.github/plugin/marketplace.json`, `.claude-plugin/marketplace.json`), so a
marketplace is not reported unreadable merely because it uses a different
layout.

When no usable token is found in an interactive terminal, `apm enroll` opens
the host's token-creation page with the name and scopes prefilled, then
prompts for the result. A pasted token is verified before use and applies to
the current command only -- export it to persist it:

```bash
export GITHUB_APM_PAT='ghp_...'    # or GITLAB_APM_PAT='glpat-...'
```

Azure DevOps and generic git hosts have no equivalent manifest endpoint, so
they skip the check and rely on whatever credentials `apm marketplace add`
already resolves.

:::note[Public marketplaces]
A public marketplace needs no credential. Because distinguishing public from
private would require an anonymous probe -- and GitHub caps unauthenticated
requests at 60/hour per IP, returning a `403` indistinguishable from a
permissions failure -- a failed credential check is a **warning, not a hard
stop**. Registration proceeds and `apm marketplace add` decides: a public
marketplace succeeds, a private one fails with its own error.
:::

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
| `1` | The source was invalid, or registration or browsing failed. |

## Non-interactive use

`apm enroll` never prompts when `APM_NON_INTERACTIVE` or `CI` is set, or when
stdin is not a TTY -- it cannot hang waiting for input.

Without a usable credential on GitHub or GitLab it warns, names the
environment variable to set, and continues, so a public marketplace still
enrolls. Registration then decides the outcome. Set a token to authenticate:

```bash
export GITHUB_APM_PAT='ghp_...'
apm enroll acme/apm-marketplace --name acme
```

## Examples

Enroll on a private GitHub marketplace:

```bash
apm enroll acme/apm-marketplace --name acme
```

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
