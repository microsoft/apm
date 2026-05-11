---
title: Registry Proxy and Air-Gapped Installs
description: Route APM downloads through a corporate HTTP proxy, JFrog Artifactory, or an internal marketplace mirror.
sidebar:
  order: 6
---

Enterprise networks rarely allow agents to reach `github.com` directly.
APM supports three layered controls for that:

1. Standard `HTTPS_PROXY` / `NO_PROXY` env vars for forward proxies.
2. `PROXY_REGISTRY_URL` for a JFrog Artifactory (or compatible) mirror
   that fronts every package download.
3. `apm marketplace add --host ...` to register internal marketplaces
   served from GHES, GHE.com, or GitLab self-managed.

For consumer-side token setup, see
[Authentication](../../consumer/authentication/) and
[Private and org packages](../../consumer/private-and-org-packages/).

## When to use what

| Goal | Mechanism |
|---|---|
| Allowlist outbound traffic at the firewall | `HTTPS_PROXY` |
| Mirror every dependency archive for audit and replay | `PROXY_REGISTRY_URL` |
| Serve internal `marketplace.json` listings from a private host | `apm marketplace add --host` |
| Fully air-gapped CI (no egress at all) | Pre-built bundle from `apm pack` |

The three compose. A typical hardened setup uses `HTTPS_PROXY` for
network egress, `PROXY_REGISTRY_URL` for dependency mirroring, and an
internal marketplace for discovery.

## Standard HTTP proxy

APM downloads packages with `requests` and `git`. Both honor the
standard env vars set by your platform team:

```bash
export HTTPS_PROXY=http://proxy.corp.example.com:8080
export HTTP_PROXY=http://proxy.corp.example.com:8080
export NO_PROXY=localhost,127.0.0.1,.corp.example.com
```

No APM-specific configuration is required. If `git clone` works against
your private repos through the proxy, `apm install` works too.

## Mirror dependencies through Artifactory

`PROXY_REGISTRY_URL` rewrites every GitHub-hosted dependency download
to fetch via Artifactory's Archive Entry Download API. Set in the
shell profile, the dev container, or CI secrets.

```bash
export PROXY_REGISTRY_URL=https://art.example.com/artifactory/github
export PROXY_REGISTRY_TOKEN=<bearer-token>   # optional
export PROXY_REGISTRY_ONLY=1                 # block direct VCS fallback
```

| Variable | Purpose |
|---|---|
| `PROXY_REGISTRY_URL` | Full proxy base including any path prefix. When set, all GitHub archives route here. |
| `PROXY_REGISTRY_TOKEN` | Bearer token sent on proxy requests. Independent of `GITHUB_APM_PAT`. |
| `PROXY_REGISTRY_ONLY` | `1` blocks direct VCS fallback at runtime and on lockfile replay. |
| `PROXY_REGISTRY_ALLOW_HTTP` | `1` silences the plaintext-token warning when the proxy is on `http://`. Use only inside an isolated network. |

Deprecated aliases `ARTIFACTORY_BASE_URL`, `ARTIFACTORY_APM_TOKEN`, and
`ARTIFACTORY_ONLY` still work but emit `DeprecationWarning`. Migrate to
the `PROXY_REGISTRY_*` names.

### Bypass prevention

When `PROXY_REGISTRY_ONLY=1`, APM refuses to fall back to `github.com`,
GHE.com, or GHES. The lockfile records `registry_prefix` for every
proxy-routed dependency. On replay, an entry pinned to a direct VCS
host aborts the install:

```text
ERROR: PROXY_REGISTRY_ONLY=1 but the following lockfile entries are
locked to direct VCS hosts and would bypass the proxy:
  - acme/security-baseline (host: github.com)
Run 'apm install --update' to re-resolve through the proxy.
```

`apm install --update` re-resolves through the active proxy and
rewrites `apm.lock.yaml`.

### Coverage

| Surface | Routed via proxy |
|---|---|
| `apm install` (GitHub-hosted deps) | Yes |
| `apm install` (Azure DevOps deps) | No -- ADO uses a different path |
| `apm install --mcp` (MCP servers) | No -- separate registry |
| `apm marketplace` (`marketplace.json` fetch) | Yes; falls back to GitHub Contents API unless `PROXY_REGISTRY_ONLY=1` |
| Policy file fetch (`apm-policy.yml`) | No -- uses the GitHub API directly |

When a surface is not proxy-routed and `PROXY_REGISTRY_ONLY=1`, APM
aborts rather than silently fetching direct.

## Internal marketplaces

A marketplace is a Git repo containing a `marketplace.json`. To point
at one hosted on GHES, GHE.com, or GitLab self-managed:

```bash
apm marketplace add acme-tools/agents \
  --host ghes.corp.example.com \
  --branch main
```

The entry is stored in `~/.apm/marketplaces.json`. Auth uses the same
PAT as private dependency installs (`GITHUB_APM_PAT`,
`GITHUB_APM_PAT_<ORG>`, or the GitLab equivalent). See
[Private and org packages](../../consumer/private-and-org-packages/).

## Cache behavior

APM keeps a local cache at `~/.apm/cache/`:

- Git checkouts (full repository clones, reused across resolves).
- HTTP cache (proxy responses, `marketplace.json` snapshots).

The proxy is upstream of the cache. A cached entry is keyed by the
resolved URL, so switching `PROXY_REGISTRY_URL` produces a fresh
download. Integrity is unchanged: every install verifies the
`content_hash` recorded in `apm.lock.yaml` regardless of where the
bytes came from. A tampered proxy that rewrites archive contents is
caught by the lockfile guard, not the cache.

Inspect or reset the cache with `apm cache info`, `apm cache prune`,
and `apm cache clean`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `RuntimeError: PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured for '<dep>'` | `PROXY_REGISTRY_ONLY=1` with no `PROXY_REGISTRY_URL`, or dep is on an unproxied host (ADO, MCP) | Set `PROXY_REGISTRY_URL`, or unset `PROXY_REGISTRY_ONLY` for that dep type |
| `ERROR: ... locked to direct VCS hosts` | Lockfile predates the proxy | `apm install --update` |
| HTTP 401/403 from the proxy | Missing or invalid `PROXY_REGISTRY_TOKEN` | Verify the token has read on the upstream repo path |
| `git clone` hangs through the proxy | `HTTPS_PROXY` not set in the env that runs `git` | Export it in the shell that invokes `apm install`; CI secrets often miss this |
| `DeprecationWarning: ARTIFACTORY_BASE_URL is deprecated` | Legacy env names | Rename to `PROXY_REGISTRY_*` |
| Plaintext-token warning on proxy startup | Token sent over `http://` | Use `https://`, or set `PROXY_REGISTRY_ALLOW_HTTP=1` if the link is internal-only |

For fully disconnected CI (no proxy reach at all), build a bundle on a
connected host with `apm pack` and restore offline. See
[Pack and distribute](../../guides/pack-distribute/).

## See also

- [Authentication](../../consumer/authentication/) -- token resolution order
- [Private and org packages](../../consumer/private-and-org-packages/) -- per-host PAT scoping
- [Pack and distribute](../../guides/pack-distribute/) -- air-gapped bundle delivery
- [Governance overview](../governance-overview/) -- policy-cache offline story
