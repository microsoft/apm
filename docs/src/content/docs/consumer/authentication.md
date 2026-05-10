---
title: Authentication
description: Install public packages with no setup; add one env var for your org's private packages.
---

Public packages on github.com just work. No token, no login, no setup.

```bash
apm install
```

You only need credentials when a dependency points at a private repo,
your company's GitHub Enterprise host, or Azure DevOps.

## The 30-second answer

Pick the path that matches your dependencies:

- **All public github.com packages.** Do nothing.
- **Private github.com / GHE.com / GHES packages.** Either run
  `gh auth login` (recommended) or set `GITHUB_APM_PAT`.
- **Azure DevOps packages.** Run `az login`, or set `ADO_APM_PAT`.

That covers the consumer case. The rest of this page expands each path.

## Already signed in with `gh`?

If you have run `gh auth login` and you can do `gh repo clone <your-org>/<repo>`,
APM picks that up automatically. There is nothing else to set.

Under the hood, APM calls `gh auth token --hostname <host>` after the
env-var lookups; if `gh` is not installed or not logged in for the host,
it is silently skipped.

## Setting `GITHUB_APM_PAT`

If you prefer an explicit token (CI, devcontainers, scripts):

```bash
export GITHUB_APM_PAT=ghp_your_token
apm install
```

Use a fine-grained or classic PAT with **read** access to the repos your
manifest references. For an org's private repos, the PAT must be
authorized for that org.

For the org-private case, see [Private and org packages](../private-and-org-packages/).

## Azure DevOps

If a dependency lives on `dev.azure.com/...`:

```bash
az login --tenant <your-tenant-id>
apm install
```

Or, if you cannot use `az`:

```bash
export ADO_APM_PAT=your_ado_pat
apm install
```

ADO is always auth-required -- there is no anonymous fallback.

## Going further

Token scopes, SSO authorization, Enterprise Managed Users (EMU), GHES
hostnames, multi-org `GITHUB_APM_PAT_{ORG}` setups, and the ADO bearer
fallback are covered in the enterprise authentication page.

For how a token is used once resolved, see
[Private and org packages](../private-and-org-packages/).
