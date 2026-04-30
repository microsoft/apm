---
title: "Registry Proxy & Air-gapped"
description: "Route APM dependency and marketplace traffic through Artifactory or a compatible proxy. Two operating modes, bypass-prevention guarantees, air-gapped CI playbook."
sidebar:
  order: 6
---

This page documents how APM routes dependency downloads through an enterprise
registry proxy (Artifactory or compatible), the trust contract that proves
traffic cannot bypass the proxy, and the playbook for fully air-gapped CI.

For the *policy-cache* offline story (a different mechanism), see
[Governance #9](../governance-guide/#9-air-gapped-and-offline).

## Why this exists

Three audiences ask the same question with different words:

- **CISO**: "Can I prove ALL dependency traffic flows through Artifactory?
  What stops a developer or a CI job from going around it?"
- **VP Engineering**: "We have standardized on Artifactory for npm and PyPI
  for a decade. Does APM fit that pattern, or is it a new exception?"
- **Platform tech lead**: "How do I roll this out across N repos? What goes
  in CI? What is the failure mode when the proxy is down?"

APM answers all three with the same mechanism: a transparent proxy layer that
rewrites GitHub-based dependency downloads to fetch via Artifactory's Archive
Entry Download API, plus a lockfile-level guard that prevents bypass.

## Operating modes

APM supports two modes. Most teams want transparent mode; explicit FQDN mode
is for repos that must pin specific dependencies to the proxy regardless of
the developer's environment.

### Mode 1: Transparent proxy (recommended)

Set environment variables. APM rewrites every GitHub-hosted dependency
download (packages and `marketplace.json`) to fetch via the proxy. No changes
to `apm.yml`.

```bash
# Required
export PROXY_REGISTRY_URL="https://art.example.com/artifactory/github"

# Optional
export PROXY_REGISTRY_TOKEN="<bearer-token>"   # sent as Authorization: Bearer
export PROXY_REGISTRY_ONLY=1                   # block all direct VCS fallback
```

| Variable | Purpose |
|---|---|
| `PROXY_REGISTRY_URL` | Full proxy URL including any path prefix (e.g. `/artifactory/github`). When set, all GitHub dependency archives are fetched from this base. |
| `PROXY_REGISTRY_TOKEN` | Optional bearer token sent on every proxy request. Composes with `GITHUB_APM_PAT` (see [Auth composition](#auth-composition)). |
| `PROXY_REGISTRY_ONLY` | When set to `1`, APM never falls back to direct VCS hosts. Combined with the lockfile guard below, this is the bypass-prevention contract. |

Apply globally (shell profile, CI secrets, dev-container env) and every
`apm install` and `apm marketplace` command in the org routes through the proxy.

:::caution
Deprecated aliases `ARTIFACTORY_BASE_URL`, `ARTIFACTORY_APM_TOKEN`, and
`ARTIFACTORY_ONLY` still work but emit a `DeprecationWarning`. Migrate to the
`PROXY_REGISTRY_*` names.
:::

### Mode 2: Explicit FQDN in `apm.yml`

Reference the proxy directly in the dependency string:

```yaml
dependencies:
  apm:
    - art.example.com/artifactory/github/acme-corp/security-baseline#v1.4.0
```

APM detects the Artifactory path and fetches via the Archive Entry Download
API for that dependency only. The rest of the manifest behaves normally.

Use this mode when:

- A specific dependency must always come from the proxy regardless of who
  runs `apm install`.
- You are publishing a template manifest that downstream consumers should
  install through your proxy without configuring environment variables.

## Bypass-prevention contract

This is the CISO trust statement. APM enforces "all traffic through the
proxy" with two cooperating mechanisms.

### 1. `PROXY_REGISTRY_ONLY=1` blocks direct fetches at runtime

When set, APM refuses to fall back to `github.com`, GitHub Enterprise Cloud,
GHES, or any other direct VCS host. If `PROXY_REGISTRY_URL` is not set or
does not match the dependency's host, the install aborts:

```
RuntimeError: PROXY_REGISTRY_ONLY is set but no Artifactory proxy is
configured for 'acme-corp/security-baseline'. Set PROXY_REGISTRY_URL or
use explicit Artifactory FQDN syntax.
```

### 2. Lockfile validation guard prevents replay-from-bypass

When a download routes through the proxy, the resulting `apm.lock.yaml`
entry pins the proxy as the source of truth:

```yaml
dependencies:
  - repo_url: acme-corp/security-baseline
    host: art.example.com
    registry_prefix: artifactory/github
    resolved_commit: a1b2c3d4...
    content_hash: "sha256:9f86d081..."
```

On every subsequent `apm install` with `PROXY_REGISTRY_ONLY=1`, APM scans
the lockfile. If any entry is locked to a direct VCS host (github.com, GHE
Cloud, GHES) instead of the proxy, the install aborts and lists the
conflicting dependencies:

```
ERROR: PROXY_REGISTRY_ONLY=1 but the following lockfile entries are
locked to direct VCS hosts and would bypass the proxy:
  - acme-corp/security-baseline (host: github.com)
  - other-org/skill-pack (host: ghes.corp.example.com)
Run 'apm install --update' to re-resolve through the proxy.
```

`apm install --update` re-resolves dependencies through the active proxy
and rewrites the lockfile.

### Trust statement (paste into procurement responses)

> When `PROXY_REGISTRY_ONLY=1` is set in CI, APM cannot install a
> dependency that did not flow through the configured proxy. Any attempt to
> install a lockfile entry pinned to a direct VCS host aborts with a
> non-zero exit code before any download occurs.

## Coverage matrix

What is and is not routed through the proxy:

| Surface | Routed via proxy | Notes |
|---|---|---|
| `apm install` (GitHub-hosted deps) | Yes | Packages from github.com, GHE Cloud, GHES |
| `apm install` (Azure DevOps deps) | **No** | ADO uses a different download path; Artifactory backends recognize GitHub/GitLab archive prefixes only |
| `apm install --mcp` | **No** | MCP servers come from a separate registry, not GitHub archives |
| `apm marketplace add` / `browse` / `search` / `update` | Yes | `marketplace.json` fetched via Archive Entry Download; falls back to GitHub Contents API unless `PROXY_REGISTRY_ONLY=1` |
| `apm pack` / `apm unpack` | N/A | Operate offline once dependencies are local; see [Air-gapped CI playbook](#air-gapped-ci-playbook) |
| Policy file fetch (`apm-policy.yml`) | **No** | Policy discovery uses the GitHub API directly. See [Governance #9](../governance-guide/#9-air-gapped-and-offline) for the policy-cache offline story. |

When `PROXY_REGISTRY_ONLY=1` is set and a surface is not proxy-routed (ADO,
MCP), APM aborts rather than silently fetching direct.

## Air-gapped CI playbook

The "fully air-gapped" story has two valid shapes. Pick based on whether CI
has network reach to the proxy.

### Shape A: CI can reach the proxy

CI is on the corp network with Artifactory access; only the public internet
is blocked.

```yaml
# .github/workflows/ci.yml
env:
  PROXY_REGISTRY_URL: https://art.corp.example.com/artifactory/github
  PROXY_REGISTRY_TOKEN: ${{ secrets.ARTIFACTORY_TOKEN }}
  PROXY_REGISTRY_ONLY: "1"

jobs:
  install:
    runs-on: self-hosted
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
      - run: apm install
      - run: apm audit --ci --policy ./vendored-policy.yml
```

`apm install` routes every dependency and `marketplace.json` fetch through
Artifactory. `apm audit --ci --policy` enforces governance from a vendored
policy file with no network calls (see [Governance #9](../governance-guide/#9-air-gapped-and-offline)).
The lockfile guard catches any entry that would bypass the proxy on
re-install.

### Shape B: CI has no network at all (bundle delivery)

CI cannot reach the proxy or the public internet. Build a bundle on a
connected host, transport it, restore it offline.

```bash
# On a connected build host (with proxy configured)
export PROXY_REGISTRY_URL=https://art.corp.example.com/artifactory/github
export PROXY_REGISTRY_ONLY=1
apm install
apm pack --format apm --archive -o ./artifacts/

# Transport ./artifacts/*.tar.gz to the air-gapped network

# In air-gapped CI (no APM, no Python, no network)
tar xzf bundle.tar.gz -C .
# Files are deployed; agents can read them immediately
```

See [Pack & Distribute](../../guides/pack-distribute/) for bundle structure
and the `apm-action` restore mode.

### Prewarming the policy cache

Independent of dependency traffic, the *policy* fetch goes direct to
GitHub. For air-gapped runs that need policy enforcement on `apm install`,
prewarm `<project_root>/apm_modules/.policy-cache/` or use
`apm audit --ci --policy <path>` as the gating check. Details in
[Governance #9](../governance-guide/#9-air-gapped-and-offline).

## Failure modes

| Symptom | Cause | Resolution |
|---|---|---|
| `RuntimeError: PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured for '<dep>'` | `PROXY_REGISTRY_ONLY=1` set but `PROXY_REGISTRY_URL` is empty, or the dep is on an unproxied host (ADO) | Set `PROXY_REGISTRY_URL`, or use explicit FQDN syntax in `apm.yml`, or unset `PROXY_REGISTRY_ONLY` for that dep type |
| `ERROR: PROXY_REGISTRY_ONLY=1 but the following lockfile entries are locked to direct VCS hosts` | Lockfile was generated before the proxy was configured | Run `apm install --update` to re-resolve through the proxy |
| HTTP 401/403 from the proxy | Missing or invalid `PROXY_REGISTRY_TOKEN`, or token lacks read on the upstream repo | Verify the token has Artifactory read on the repository being fetched |
| Proxy unreachable (timeout, DNS) with `PROXY_REGISTRY_ONLY=1` | Proxy down, network partition | Install fails closed. Restore proxy connectivity or fall back to a pre-built [bundle](../../guides/pack-distribute/) |
| `DeprecationWarning: ARTIFACTORY_BASE_URL is deprecated` | Using legacy env-var names | Rename to `PROXY_REGISTRY_*`. Old names continue to work but will be removed in a future major release |
| Warning: lockfile entry locked to proxy is missing `content_hash` | Older proxy-routed entry without integrity hash | Run `apm install --update` to populate. Without `content_hash`, a tampered proxy could redirect downloads without detection |

## Auth composition

`PROXY_REGISTRY_TOKEN` and the GitHub PAT (`GITHUB_APM_PAT`, `GITHUB_TOKEN`,
`GH_TOKEN`) are independent and used for different request paths:

- Requests to `PROXY_REGISTRY_URL` send `Authorization: Bearer
  <PROXY_REGISTRY_TOKEN>`.
- Requests to `github.com` / GHE / GHES (only possible when
  `PROXY_REGISTRY_ONLY` is unset) use the GitHub PAT.

In a hybrid setup where `PROXY_REGISTRY_ONLY` is unset and some dependencies
fall back to direct GitHub (because they are not mirrored), both tokens are
used: proxy traffic auths with the bearer, direct traffic auths with the
PAT. Set both in CI secrets if you support hybrid.

For strict environments, set `PROXY_REGISTRY_ONLY=1` and only configure
`PROXY_REGISTRY_TOKEN`. The GitHub PAT is then unused at install time.

## HTTP proxies

The proxy can be served over HTTP, but APM treats this as an insecure
dependency channel. The same approval surface applies as for any HTTP
dependency: see [HTTP (insecure) dependencies](../security/#http-insecure-dependencies).
Production deployments should always use HTTPS.

## See also

- [Governance #9](../governance-guide/#9-air-gapped-and-offline) -- offline policy enforcement (different mechanism)
- [Security Model](../security/) -- attack surface, content scanning, HTTP dep handling
- [Pack & Distribute](../../guides/pack-distribute/) -- bundle delivery for fully disconnected CI
- [Marketplaces](../../guides/marketplaces/) -- marketplace command surface
