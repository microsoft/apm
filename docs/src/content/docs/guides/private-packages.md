---
title: "Private Packages"
description: "Create and distribute private APM packages within your team or organization."
sidebar:
  order: 9
---

A private APM package is just a private git repository with an `apm.yml`. There is no registry and no publish step — make the repo private, grant read access, and `apm install` handles the rest.

## Create the package

```bash
apm init my-private-package && cd my-private-package
# add content to .apm/instructions/, .apm/prompts/, etc.
git init && git add . && git commit -m "Initial package"
git remote add origin https://github.com/your-org/my-private-package.git
git push -u origin main
```

Set the repository to **private** in your git host's settings.

## Install it

Set the appropriate token (see [Authentication](../../getting-started/authentication/)), then install like any public package:

```bash
export GITHUB_APM_PAT=github_pat_your_token
apm install your-org/my-private-package
```

Or declare it in `apm.yml`:

```yaml
dependencies:
  apm:
    - your-org/my-private-package#v1.0.0
```

For GitLab, Bitbucket, or self-hosted git servers, use the [`git:` object form](../dependencies/) and rely on your [existing git credentials](../../getting-started/authentication/):

```yaml
dependencies:
  apm:
    - git: git@gitlab.com:acme/private-standards.git
      ref: v1.0.0
```

Self-hosted servers that use non-default git ports (e.g. Bitbucket Datacenter on SSH port 7999) are supported — specify the port in the URL. Use the `ssh://` form, not SCP shorthand, since `git@host:path` cannot carry a port:

```yaml
dependencies:
  apm:
    - git: ssh://git@bitbucket.example.com:7999/project/repo.git
      ref: v1.0.0
    - git: https://git.internal:8443/team/repo.git      # custom HTTPS port
```

APM reuses the same port across protocols during clone fallback (so `ssh://host:7999/...` falls back to `https://host:7999/...`). If your host serves SSH and HTTPS on different ports and SSH is unreachable, pin the protocol that matches the port you need.

## Share with your team

Every developer needs read access to the private repository and the appropriate token in their environment. For teams, a fine-grained PAT scoped to the organization works well — no write access required.

## Use in CI/CD

Inject the token as a secret:

```yaml
# GitHub Actions
- uses: microsoft/apm-action@v1
  env:
    GITHUB_APM_PAT: ${{ secrets.GITHUB_APM_PAT }}
```

See the [CI/CD guide](../../integrations/ci-cd/) for Azure Pipelines and other systems.

## Org-wide private packages

For centrally-maintained standards packages that stay private, see [Org-Wide Packages](../../guides/org-packages/) — the pattern is identical, just keep the repository private and distribute access via your org-scoped token.
