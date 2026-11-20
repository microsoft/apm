---
title: Use private sources
description: Install packages from private GitHub, GitHub Enterprise, Azure DevOps, GitLab and other git hosts, and get through corporate proxies and TLS inspection.
---

Use this page when your packages live in private repositories or on an
enterprise host. Public packages on `github.com` need no setup. For anything
else, APM reuses credentials you already have (the GitHub CLI, the Azure CLI,
your git credential helper) or one environment variable.

## Start with `apm doctor`

```bash frame="terminal"
apm doctor
apm auth github.com
```

`apm doctor` checks git, network access and whether a token is present.
`apm auth HOST` names the credential APM will use for a GitHub or GitLab host
and walks you through creating one if there is none:

```text frame="none"
[+] github.com: using credential from GH_TOKEN.
[i] Add --check to validate it against the API.
```

## How APM picks a credential

For a public `github.com` package, APM tries without credentials. On a 401,
403 or 404, and for every other host, it walks the chain for that kind of
host and uses the earliest credential it finds:

| Host | Credential order |
|---|---|
| `github.com`, `*.ghe.com`, GitHub Enterprise Server | `GITHUB_APM_PAT_<ORG>`, `GITHUB_APM_PAT`, `GITHUB_TOKEN`, `GH_TOKEN`, `gh auth token`, git credential helper |
| Azure DevOps Services (`dev.azure.com`, `*.visualstudio.com`) | `ADO_APM_PAT`, Azure CLI (`az`) sign-in, git credential helper |
| Azure DevOps Server | `ADO_APM_PAT`, git credential helper |
| `gitlab.com` and registered GitLab hosts | `GITLAB_APM_PAT`, `GITLAB_TOKEN`, git credential helper |
| Any other host (Bitbucket, Gitea, your own server) | git credential helper |

A token for one kind of host is never sent to another: a `GITHUB_TOKEN` set
in CI never reaches GitLab. APM sends credentials as an HTTPS header, never
in a URL, and does not store them. Every variable:
[Environment variables](../../reference/environment-variables/).

## GitHub and GitHub Enterprise

If `gh repo clone contoso/review-skills` works, `apm install` works: APM asks
the GitHub CLI for its token. For scripts and CI, set a token instead:

```bash frame="terminal"
export GITHUB_APM_PAT=github_pat_...
```

Create a fine-grained token with **Contents: Read** and **Metadata: Read**,
owned by the organization that owns the repositories (a token owned by your
user cannot read organization repositories). Authorize it for SSO if the
organization requires it.

When packages come from several organizations, set one token each. The suffix
is the organization name in upper case with hyphens as underscores:

```bash frame="terminal"
export GITHUB_APM_PAT_CONTOSO=github_pat_...
export GITHUB_APM_PAT_CONTOSO_LABS=github_pat_...
```

**GitHub Enterprise Cloud with data residency** (`*.ghe.com`) has no public
repositories, so APM goes straight to credentials. Write the host in the
reference: `apm install contoso.ghe.com/platform/review-skills`.

**GitHub Enterprise Server**: make shorthand resolve to your server with
`default_host:` in `apm.yml` (committed) or `GITHUB_HOST` (per machine), and
write `github.com/owner/repo` for packages on GitHub:

```diff title="apm.yml"
+default_host: github.contoso.com
 dependencies:
   apm:
     - platform/review-skills
+    - github.com/microsoft/apm-sample-package#v1.0.0
```

Enterprise Managed User tokens cannot read public `github.com` repositories;
public packages install without one.

### Multi-account Git Credential Manager

If you use several GitHub accounts, let Git Credential Manager pick one per
organization. APM sends the repository path with each request:

```bash frame="terminal"
git config --global credential.useHttpPath true
git config --global credential.https://github.com/contoso.username ana-contoso
```

If an account picker still appears, run `apm install --verbose` and compare
the `path=` APM logs with your config entry.

## Azure DevOps

Sign in with the Azure CLI, or set a personal access token with **Code:
Read**:

```bash frame="terminal"
az login --tenant <tenant-id>
apm install dev.azure.com/contoso/platform/review-skills
```

```bash frame="terminal"
export ADO_APM_PAT=...
```

References use `org/project/repo`; use the `_git` form for a subfolder and
encode spaces as `%20`:

```bash frame="terminal"
apm install dev.azure.com/contoso/platform/_git/review-skills/skills/security#v2.0.0
```

APM tries `ADO_APM_PAT`, then an Entra ID token from `az`, then your git
credential helper. A rejected PAT falls back to `az` with a warning to unset
the stale variable. When the credential came from Git Credential Manager, APM
reports success or a 401 back to it, so an expired credential is replaced.
Azure DevOps has no anonymous access; if every step fails, the error lists
what each one returned.

### On-prem Azure DevOps Server

Register the host so APM uses the Azure DevOps chain:

```bash frame="terminal"
export ADO_HOST=ado.contoso.com          # or APM_ADO_HOSTS=host1,host2
export ADO_APM_PAT=...
apm install ado.contoso.com/DefaultCollection/Platform/_git/review-skills
```

Set hostnames, with no scheme or port; put a non-default port in the dependency
URL. Azure DevOps Server does not use `az`. A server under a path prefix such
as `/tfs/` is not supported.

## GitLab (SaaS and self-managed)

```bash frame="terminal"
export GITLAB_APM_PAT=glpat-...
```

Use a token with `read_repository` scope. `gitlab.com` is recognized
automatically. Register a self-managed host with `GITLAB_HOST` (one host) or
`APM_GITLAB_HOSTS` (comma-separated) so it gets GitLab tokens and API calls.
For a single dependency on an unrecognized host, `type: gitlab` on its
object-form entry selects GitLab's API without sending it your tokens.

## Other git hosts and SSH

For Bitbucket, Gitea or your own server, APM uses the same git credential
helper as your shell. If `git clone https://git.contoso.com/team/repo.git`
works, `apm install` works.

For an SSH dependency (`git@host:org/repo.git`), APM uses your ssh-agent and
`~/.ssh/config` without prompting, so load the key beforehand (`ssh-add`). Prefer
SSH for all shorthand with `--ssh` or `apm config set prefer-ssh true`; see
[Transport selection](../../consumer/install-packages/#transport-selection).

### Generic marketplace Git transport

APM downloads marketplaces from generic git hosts without prompting:

| Remote | Credentials |
|---|---|
| HTTPS | Your git credential helper; platform token variables are removed |
| HTTP | None |
| SSH | Your key, no tokens, 30-second connect timeout |
| GitHub, GitLab, Azure DevOps | The host's chain above |

### Git URL rewrite safety

APM honors your `url.<base>.insteadOf` rules when they stay on the same host,
such as HTTPS to SSH or a local mirror. It refuses rewrites that add
credentials, downgrade to `http://` or `git://`, or change host. To find the
rule involved:

```bash frame="terminal"
git config --show-origin --get-regexp '^url\..*\.insteadOf$'
```

## Corporate CA and proxies

If your network inspects TLS, add your company's CA to APM's HTTPS trust. It
keeps the operating system's roots:

```bash frame="terminal"
export APM_EXTRA_CA_BUNDLE=/path/to/corporate-ca.pem
```

Git uses its own settings (`git config http.sslCAInfo`). Proxy and
certificate details: [SSL / TLS issues](../../troubleshooting/ssl-issues/).
To install through an Artifactory or other package proxy, see
[Install through a proxy or offline](../../enterprise/registry-proxy/).

### TLS-inspecting proxy blocks the anonymous attempt

The initial, credential-free attempt to `github.com` can fail at TLS (for
example `CRYPT_E_NO_REVOCATION_CHECK` on Windows) while a plain `git clone`
works. Send your credential on that attempt instead:

```bash frame="terminal"
apm install --auth-first
```

Make it permanent with `APM_GITHUB_AUTH_FIRST=1` or
`apm config set github-auth-first true`. Other hosts are not affected.

## Verify

```bash frame="terminal"
apm auth github.com --check
apm install --verbose
```

`--check` validates the token against the host's API. `--verbose` logs which
source each host used, for example `token from ADO_APM_PAT`.

## When things go wrong

- **404 for a repository you can open in the browser.** The token cannot see
  it: check the token's owner, repository access and SSO authorization.
- **Rate limited on `github.com`.** Set any valid `GITHUB_TOKEN`.
- **Credential helper times out.** Raise `APM_GIT_CREDENTIAL_TIMEOUT` (seconds,
  default 60, maximum 180).
- **SSH hangs on a corporate network.** Port 22 is blocked; use `--https`.
- **More:** [Install failures](../../troubleshooting/install-failures/).

:::tip[Other options]
Tokens in CI: pass them as secrets in the job environment
([Run in CI](../../consumer/run-in-ci/)). Package registries
(Experimental) use `APM_REGISTRY_TOKEN_<NAME>`; see
[Environment variables](../../reference/environment-variables/).
:::
