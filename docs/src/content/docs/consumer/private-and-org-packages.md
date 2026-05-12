---
title: Private and org packages
description: Spec a private dependency in apm.yml so install can fetch it from GitHub, GHE, EMU, Azure DevOps, or GitLab.
---

You declare a private dependency the same way you declare a public
one. Only the host and the token change. For token setup, see
[Authentication](../authentication/).

## The shapes of a dependency ref

`apm install` accepts these forms in `apm.yml` and on the command line:

```yaml
dependencies:
  apm:
    # 1. Shorthand, github.com is implicit
    - acme/standards#v1.2.0

    # 2. Shorthand with explicit host (GHE Cloud, GHES, GitLab)
    - acme.ghe.com/acme/standards#v1.2.0
    - gitlab.com/acme/standards#v1.2.0

    # 3. Azure DevOps shorthand: org/project/repo
    - dev.azure.com/acme-org/platform/standards#v1.2.0

    # 4. Object form (any git URL, any port, custom protocol)
    - git: ssh://git@bitbucket.acme.com:7999/team/standards.git
      ref: v1.2.0

    # 5. Local path (file:// equivalent for unpacked bundles)
    - ./vendor/standards
```

Use shorthand when the host follows owner/repo (or ADO's
org/project/repo). Use the object form for custom ports, non-standard
schemes, or deep GitLab subgroups shorthand cannot disambiguate. Full
grammar: `DependencyReference.parse` in
`src/apm_cli/models/dependency/reference.py`.

## GitHub.com private repos

Make the repo private, give your token read access, reference it like
any other dep:

```yaml
dependencies:
  apm:
    - acme/private-standards#v1.0.0
```

Token: `GITHUB_APM_PAT`, a fine-grained PAT with read access on the
org. For manifests that span multiple orgs, scope per-org with
`GITHUB_APM_PAT_<ORG>` (uppercase; hyphens become underscores). Per-org
vars win over the global one.

```bash
export GITHUB_APM_PAT_ACME=ghp_acme...
export GITHUB_APM_PAT_PARTNER_CO=ghp_partner...
apm install
```

## GitHub Enterprise (GHE Cloud and GHES)

GHE Cloud (Data Residency) hosts end in `.ghe.com` and are recognized
automatically. GHES (self-hosted) needs `GITHUB_HOST` so APM knows the
FQDN is GitHub-flavoured:

```bash
export GITHUB_HOST=github.acme.internal
export GITHUB_APM_PAT=ghp_...
```

```yaml
dependencies:
  apm:
    - acme.ghe.com/platform/standards#v1.2.0       # GHE Cloud
    - github.acme.internal/platform/standards#v1   # GHES
```

EMU (Enterprise Managed Users) does not change the dep ref grammar; it
only tightens what tokens can do (classic-only PATs, mandatory SSO
authorization, SAML on every clone). Token scoping and SSO steps live
in the enterprise authentication guide.

## Azure DevOps

ADO repos use a three-segment path (`org/project/repo`) and the
`dev.azure.com` host:

```yaml
dependencies:
  apm:
    - dev.azure.com/acme-org/platform/standards#v1.2.0
```

Token: `ADO_APM_PAT`, or `az login --tenant <id>` (APM picks up the
Azure CLI bearer). ADO is always auth-required -- no anonymous
fallback. Azure DevOps Server (on-prem) works the same way once you
set `GITHUB_HOST` to its FQDN.

## GitLab

`gitlab.com` shorthand and any GitLab-flavoured FQDN you opt into:

```yaml
dependencies:
  apm:
    - gitlab.com/acme/standards#v1.2.0
    - gitlab.acme.internal/platform/standards#v1.2.0
```

Tokens, in precedence order: `GITLAB_APM_PAT`, `GITLAB_TOKEN`, then
your git credential helper. Self-managed GitLab needs `GITLAB_HOST`
(single host) or `APM_GITLAB_HOSTS` (comma-separated):

```bash
export GITLAB_HOST=gitlab.acme.internal
export APM_GITLAB_HOSTS=gitlab.acme.internal,gitlab.partner.io
```

Nested groups deeper than `group/subgroup/repo` cannot always be
disambiguated by shorthand -- use the object form:

```yaml
dependencies:
  apm:
    - git: https://gitlab.com/acme/platform/team/standards.git
      ref: v1.2.0
```

## Custom ports and self-hosted git

Use the object form for non-default ports. Use `ssh://` -- SCP
shorthand (`git@host:path`) cannot carry a port:

```yaml
dependencies:
  apm:
    - git: ssh://git@bitbucket.acme.internal:7999/team/standards.git
      ref: v1.2.0
    - git: https://git.acme.internal:8443/team/standards.git
      ref: v1.2.0
```

APM falls back across protocols on the same port: `ssh://host:7999`
will retry as `https://host:7999/...` if SSH is unreachable.

## Pre-fetched bundles (offline / air-gapped)

Install a packed bundle from disk:

```yaml
dependencies:
  apm:
    - ./vendor/acme-standards
```

Full pack-and-unpack workflow: [Deploy a bundle](../deploy-a-bundle/).

## Marketplaces

ADO and GitLab marketplaces use the same auth backends as direct deps
-- once `ADO_APM_PAT` or `GITLAB_APM_PAT` is set, marketplace fetches
authenticate. See [Marketplaces](../../guides/marketplaces/) for the
consumer-side workflow.

## Out of scope

- Token scopes, SSO authorization, EMU classes, Azure CLI tenants:
  enterprise authentication guide.
- Lockfile pinning: [Manage dependencies](../manage-dependencies/).
- Resolve / verify / integrate phases: [Lifecycle](../../concepts/lifecycle/).
- CLI flags: [CLI reference](../../reference/cli/install/).
