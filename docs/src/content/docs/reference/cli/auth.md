---
title: apm auth
description: Get a working credential for a git host, and create one if there is none.
sidebar:
  order: 30
---

## Synopsis

```bash
apm auth HOST [--check] [--export] [--verbose]
```

## Description

`apm auth` does one job: make sure you have a token APM can use for a git
host, walking you through creating one if you do not.

It does **not** register marketplaces or install packages. Use
[`apm marketplace add`](../marketplace/) and [`apm install`](../install/) for
that -- they report their own errors, and a second pre-flight check here would
only duplicate them.

`HOST` is a host name (`github.com`, `gitlab.com`, `ghe.corp.example`), not a
repository or marketplace URL.

## How APM resolves tokens

Environment variables are consulted **before** any git credential helper, so
exporting one is sufficient -- no git configuration change is required.

| Host | Resolution order | Scopes |
|---|---|---|
| GitLab | `GITLAB_APM_PAT`, `GITLAB_TOKEN`, git credential helper | `read_repository,read_api` |
| GitHub / GHES | `GITHUB_APM_PAT`, `GITHUB_TOKEN`, `gh auth token`, git credential helper | `repo` |

Azure DevOps and generic git hosts resolve differently and have no token page
to point at; `apm auth` exits non-zero for them. Use [`apm doctor`](../doctor/)
to inspect those.

## Setting the token in your shell

A command cannot change its parent shell's environment, and APM reads
credentials only from environment variables and git credential helpers --
never from `~/.apm/config.json`. So `apm auth` prints the line you need
rather than saving the token somewhere APM would not read it back:

```bash
eval "$(apm auth gitlab.com --export)"
```

With `--export`, **stdout carries only the `export` line** and all narration
goes to stderr, which is what makes that `eval` safe. The token value is
shell-quoted.

:::caution
`--export` prints your token to the terminal, so it can land in scrollback
and shell history. Prefer plain `apm auth <host>` when you only want to know
whether a credential is present.
:::

## Validating a token

By default `apm auth` reports which credential APM *resolves* -- it does not
validate it, which costs a network round trip (and unauthenticated GitHub
requests are capped at 60/hour per IP). Pass `--check` to verify it against
the host's REST API:

```bash
apm auth gitlab.com --check
```

This distinction matters on GitLab. A token that works for `git clone` is not
automatically valid for the REST API that marketplace lookups use: an **OAuth
session token** -- what `glab auth login` leaves behind -- returns `401` from
the API. `--check` names that specific failure instead of leaving you with a
confusing downstream error.

## Options

| Flag | Description |
|---|---|
| `--check` | Validate the token against the host's REST API (one network call). |
| `--export` | Print `export VAR=token` on stdout for `eval`; narration goes to stderr. |
| `-v`, `--verbose` | Show detailed output. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | A credential is available (and valid, with `--check`). |
| `1` | No usable credential, `HOST` was not a host name, or the host has no token flow. |

## Non-interactive use

`apm auth` never prompts when `APM_NON_INTERACTIVE` or `CI` is set, or when
stdin is not a TTY. It exits `1` naming the variable to set, rather than
hanging.

## Examples

Check what APM resolves for GitHub:

```bash
apm auth github.com
```

Validate a GitLab token against the API:

```bash
apm auth gitlab.com --check
```

Set the token in the current shell:

```bash
eval "$(apm auth gitlab.com --export)"
```

Then register a marketplace and install from it:

```bash
apm marketplace add gitlab.com/acme/team/apm-marketplace --name acme
apm install <plugin-name>@acme --target claude
```

:::note[Shadowing credential helpers]
On macOS, the system `osxkeychain` helper is unscoped: a stale entry for a
host answers `git credential fill` before any scoped helper and keeps winning.
`apm auth` detects this and prints the command to clear it. It never erases
credentials for you.
:::

## Related

- [`apm marketplace`](../marketplace/) -- register and browse marketplaces.
- [`apm doctor`](../doctor/) -- diagnose git, network, and authentication
  problems.
- [Authentication](../../../getting-started/authentication/) -- the full
  credential model.
