---
title: Authentication
description: Install public packages with no setup; add one env var or use your git credential helper for any private host -- GitHub, GitLab, Azure DevOps, Bitbucket, Gitea, or self-hosted.
---

APM resolves dependencies from any git host you can `git clone` from. Public packages on github.com need zero setup. Other hosts -- GitHub Enterprise, GitLab (SaaS or self-managed), Azure DevOps, Bitbucket, Gitea, or any self-hosted git server -- need either one env var or your existing git credential helper.

```bash
apm install
```

## The 30-second answer

Pick the path that matches your dependencies:

- **All public github.com packages.** Do nothing.
- **Private github.com / GHE.com / GHES packages.** Either run `gh auth login` (recommended) or set `GITHUB_APM_PAT`.
- **GitLab packages (SaaS or self-managed).** Set `GITLAB_APM_PAT`, or rely on your `git credential` helper.
- **Azure DevOps packages.** Run `az login`, or set `ADO_APM_PAT`.
- **Bitbucket, Gitea, or any other git host.** Use your existing `git credential` helper -- if `git clone <url>` works in your shell, `apm install` works too.

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

## GitLab (SaaS or self-managed)

If a dependency lives on `gitlab.com` or your self-hosted GitLab:

```bash
export GITLAB_APM_PAT=glpat_your_token
apm install
```

Use a project- or group-scoped token with **read_repository** scope. Self-managed GitLab works with the same env var; APM resolves the host from the dependency URL.

If you have configured a git credential helper for GitLab (e.g. `git credential-manager` on Windows / macOS), APM falls back to it after the env-var lookup -- you do not need `GITLAB_APM_PAT` if `git clone https://gitlab.com/<your-group>/<repo>` already prompts you once and caches.

`GITLAB_TOKEN` is also accepted as a lower-precedence fallback for compatibility with CI environments that already set it.

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

## Bitbucket, Gitea, and any other git host

For any git host APM does not have a platform-specific PAT for -- Bitbucket Cloud or Server, Gitea, self-hosted Forgejo, an enterprise git mirror -- APM resolves credentials through the same `git credential` helper your shell uses.

```bash
git clone https://your-host.example.com/team/repo.git  # cache the credential once
apm install                                             # APM picks up the cached credential
```

There is no APM-specific env var for these hosts by design: if you can `git clone`, APM can install. Configure your credential helper once (`git credential-manager`, Keychain, libsecret, plain-text store -- whatever your shell uses) and `apm install` follows the same path git already trusts.

For non-interactive environments (CI, devcontainers), set the credential through your CI's secret store and configure `git config --global credential.helper store` against a runtime-injected `~/.git-credentials` file.

## Going further

Token scopes, SSO authorization, Enterprise Managed Users (EMU), GHES hostnames, multi-org `GITHUB_APM_PAT_{ORG}` setups, GitLab self-managed FQDN routing, and the ADO bearer fallback are covered in the [enterprise authentication](../../enterprise/security/) page.

For how a token is used once resolved, see [Private and org packages](../private-and-org-packages/).
