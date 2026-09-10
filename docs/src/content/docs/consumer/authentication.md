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
- **Azure DevOps Services packages.** Set `ADO_APM_PAT`, or run `az login`;
  APM checks the PAT first.
- **Azure DevOps Server packages.** Register the host with `ADO_HOST` or
  `APM_ADO_HOSTS`, then set `ADO_APM_PAT`. Server does not use the Azure CLI
  bearer.
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

## SSH clone prerequisites

APM runs git clones non-interactively. Before using an SSH dependency, make
sure its key is already available to SSH. Unlock a passphrase-protected key
first (for example, with `ssh-add <key-file>`). In CI, load a dedicated deploy
key non-interactively or use token-backed HTTPS.

## GitLab (SaaS or self-managed)

GitLab `path:` single-file fetches use sparse/partial Git checkout and the
same [transport policy](../manage-dependencies/#transport-selection) as clones.
SSH keys and Git credential helpers work without an extra token, even when
the REST API is disabled.

In strict mode, explicit SSH/SCP URLs and SSH preference keep these fetches
on SSH, preserving the user, host, port, and ref. Git failure never triggers
REST, even with a PAT available. Fix SSH access or explicitly declare the
HTTPS web endpoint; APM does not map SSH aliases to web hostnames.

REST fallback runs only after the selected Git plan is exhausted and an
executed attempt used effective HTTPS with the same normalized scheme,
host, and port as the API endpoint. Default HTTPS fallback remains supported.
HTTP is not automatically upgraded to HTTPS; an HTTPS URL rewritten by Git
to SSH or a local mirror does not authorize REST.

For token-backed HTTPS:

```bash
export GITLAB_APM_PAT=glpat_your_token
apm install
```

Use **read_repository** scope. APM checks `GITLAB_APM_PAT`, then
`GITLAB_TOKEN`, then the Git credential helper for trusted GitLab hosts.
For self-managed host registration and token trust, see
[GitLab authentication](../../getting-started/authentication/#gitlab-saas-and-self-managed).

## Azure DevOps

For Azure DevOps Services (`dev.azure.com` and `*.visualstudio.com`), set a
PAT or use an active Azure CLI session:

```bash
export ADO_APM_PAT=your_ado_pat
apm install
```

```bash
az login --tenant <your-tenant-id>
apm install
```

For Azure DevOps Server, register the host and set a PAT:

```bash
export ADO_HOST=ado.example.com
export ADO_APM_PAT=your_ado_pat
apm install
```

Use `APM_ADO_HOSTS` instead when you have multiple Server instances. Server
is PAT-only; the Azure CLI bearer does not apply. ADO is always
auth-required -- there is no anonymous fallback. See the
[full Azure DevOps flow](../../getting-started/authentication/#azure-devops).

## Bitbucket, Gitea, and any other git host

For any git host APM does not have a platform-specific PAT for -- Bitbucket Cloud or Server, Gitea, self-hosted Forgejo, an enterprise git mirror -- APM resolves credentials through the same `git credential` helper your shell uses.

```bash
git clone https://your-host.example.com/team/repo.git  # cache the credential once
apm install                                             # APM picks up the cached credential
```

There is no APM-specific env var for these hosts by design: if you can `git clone`, APM can install. Configure your credential helper once (`git credential-manager`, Keychain, libsecret, plain-text store -- whatever your shell uses) and `apm install` follows the same path git already trusts.

For non-interactive environments (CI, devcontainers), set the credential through your CI's secret store and configure `git config --global credential.helper store` against a runtime-injected `~/.git-credentials` file.

## Marketplace transport

For in-repository plugins from GitLab and generic git marketplaces, an SSH
registration stays SSH when APM generates the concrete `git:` and `path:`
dependency. Existing SSH keys keep working instead of the dependency being
rewritten to HTTPS. See
[Installing from marketplaces](../installing-from-marketplaces/#local-and-self-hosted-marketplaces).

## Going further

Token scopes, SSO authorization, Enterprise Managed Users (EMU), GHES
hostnames, multi-org `GITHUB_APM_PAT_{ORG}` setups, GitLab self-managed FQDN
routing, and the Azure DevOps Services and Server credential flows are
covered in the [authentication guide](../../getting-started/authentication/).

For how a token is used once resolved, see [Private and org packages](../private-and-org-packages/).
