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

:::note[Not to be confused with **Registries**]
The **registry proxy** documented here transparently fronts an upstream Git
host (GitHub, GitLab) so dependency clones flow through your enterprise
infrastructure. Configured per-machine via `PROXY_REGISTRY_*` env vars.

A **dedicated registry** ([Registries guide](../guides/registries/)) is a
separate, additive package source that speaks the [Registry HTTP API](../reference/registry-http-api/)
directly — no Git host upstream. Configured per-project in `apm.yml` via the
top-level `registries:` block, and currently requires `apm experimental enable registries`.

Both can be used together; they're orthogonal.
:::

For the *policy-cache* offline story (a different mechanism), see
[Governance #9](./governance-guide/#9-air-gapped-and-offline).

For consumer-side token setup, see
[Authentication](../consumer/authentication/) and
[Private and org packages](../consumer/private-and-org-packages/).

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

### Nested-group repos (GitLab subgroups behind the proxy)

GitHub uses a fixed `owner/repo` shape, but GitLab projects can sit at any
subgroup depth (e.g. `group/subgroup/project`). When
`PROXY_REGISTRY_ONLY=1` is set, APM treats path segments past the second
as part of the repo slug; the real boundary between repo path and
in-repo virtual sub-path is then settled at install time by the same
deterministic boundary probe used for explicit FQDN deps (see
[Explicit Artifactory FQDN](#explicit-artifactory-fqdn-deterministic-boundary-probe)
below):

```yaml
# apm.yml -- 3-segment GitLab project behind a registry proxy
dependencies:
  apm:
    - group/subgroup/project#main          # resolves to the full nested path
    - group/sub-a/sub-b/project#v1.2.0     # arbitrary depth supported
```

Virtual sub-paths under nested-group repos work via the probe: parse
defaults to all-as-repo, then the install-time resolver HEAD-probes
candidate splits against the proxy and rebuilds the dependency
reference at the first split whose archive responds 2xx-3xx:

```yaml
dependencies:
  apm:
    # The probe walks shallow-first and lands on the real boundary --
    # ``group/subgroup/project`` is the repo, ``skills/<name>`` is the
    # virtual sub-path -- no marker-segment heuristic involved.
    - group/subgroup/project/skills/<name>
    # Files ending in ``.prompt.md`` / ``.instructions.md`` /
    # ``.chatmode.md`` / ``.agent.md`` are structurally a virtual file
    # at parse time; the probe still confirms which directory the file
    # sits under is part of the repo path.
    - group/subgroup/project/<name>.prompt.md
```

Probe authentication matches the URL being probed: bare-shorthand deps
(Mode 2) use the proxy's own bearer token from `PROXY_REGISTRY_TOKEN`,
while explicit-FQDN deps (Mode 1) use the per-host auth resolver -- in
both cases the audience matches the probed URL, never the upstream Git
host.

#### Trade-off: lockfile env-dependence

The fold-into-repo behavior is gated on `PROXY_REGISTRY_ONLY` to keep the
legacy two-segment shape for direct installs. Consequence: the same
shorthand parses differently with vs. without the env set. For a team
that always runs through the proxy, this is invisible. For a mixed CI
fleet, expect lockfile drift if some agents have the env and others
don't -- pin the env in the same place you pin Python and APM versions.

#### Configuring the upstream remote (GitLab)

When the proxy fronts a private GitLab instance, the proxy itself must
authenticate upstream -- the client (APM) does not need a token if the
proxy is configured to accept anonymous reads on its API.

In the Artifactory UI, for the remote pointing at GitLab:

| Field | Value |
|---|---|
| URL | `https://<gitlab-host>` (no path prefix) |
| Repository Path Prefix | *blank* (any value gets prepended to every upstream request) |
| Username | empty *or* the GitLab username |
| Password / Token | the raw GitLab PAT value -- no `PRIVATE-TOKEN:` prefix |
| Token Authentication | enable when the password is a GitLab PAT |
| VCS Provider | `GitLab` |

The PAT must carry **`read_repository`** scope -- `read_api` alone does
not permit `/-/archive/` downloads. Verify directly against GitLab
before saving on the remote:

```bash
curl -sI -H "PRIVATE-TOKEN: $PAT" \
  "https://<gitlab-host>/<group>/<project>/-/archive/<ref>/<basename>-<ref>.zip" \
  | head -3
# Want: HTTP/1.1 200 OK + Content-Type: application/zip
```

#### Default branch gotcha

APM defaults to `main` when no ref is provided. GitLab projects whose
default branch is still `master` will return HTTP 404 for every archive
URL APM tries. Pin the ref in `apm.yml` (`<repo>#master`) when the
project hasn't been renamed.

#### Explicit Artifactory FQDN: deterministic boundary probe

When a dep is written with the full proxy URL --
`<host>/artifactory/<key>/<owner>/<repo>[/<more>]` -- parse time gives a
simple `owner / first-segment / rest-as-virtual` split. The real
boundary is settled at install time by an authoritative resolver that
mirrors APM's native GitLab probing pattern, without a separate metadata
API:

1. Enumerate every plausible `(owner, repo, virtual_path)` split
   shallow-first.
2. `HEAD` each candidate's archive URL on the proxy (no follow on
   redirects, so the bearer token can't leak cross-host).
3. The first candidate that responds 2xx-3xx wins; the dependency
   reference is rebuilt at that boundary and persisted to `apm.yml` as
   a structured `git:` + `path:` entry.

If every candidate is rejected the resolver raises -- there is no
silent fallback to the parse-time guess:

| Result | Behaviour |
|---|---|
| Single candidate (e.g. `host/artifactory/key/owner/repo`) | Parse-time ref returned unchanged; no HEAD probe issued. |
| All candidates `4xx` (excluding 401/403) | `ValueError: ... did not resolve to a reachable repository archive` |
| All candidates `401`/`403` | `ValueError: ... authentication problem, not a missing repo` -- check the token's read scope. |

To opt out of probing -- e.g. when the proxy is offline at install time
or when you want a deterministic byte-for-byte string -- use the
explicit `//` boundary marker, which short-circuits the resolver to a
single candidate:

```text
<host>/artifactory/<key>/<owner>/<deep>/<slug>//<virtual/path>
```

When a surface is not proxy-routed and `PROXY_REGISTRY_ONLY=1`, APM
aborts rather than silently fetching direct.

## Internal marketplaces

A marketplace is a Git repo containing a `marketplace.json`. To point
at one hosted on GHES, GHE.com, or GitLab self-managed:

```bash
apm marketplace add acme-tools/agents \
  --host ghes.corp.example.com \
  --ref main
```

The entry is stored in `~/.apm/marketplaces.json`. Auth uses the same
PAT as private dependency installs (`GITHUB_APM_PAT`,
`GITHUB_APM_PAT_<ORG>`, or the GitLab equivalent). See
[Private and org packages](../consumer/private-and-org-packages/).

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
| `Invalid zip archive` with a body that starts `<!DOCTYPE html>` and is ~17KB | Upstream returned a sign-in page; proxy cached the HTML | Configure upstream credentials on the registry remote, purge the cache, then refetch |
| 3-segment dep (`group/sub/project`) fails with HTTP 404 from the proxy | APM treated `project` as a virtual sub-path | Set `PROXY_REGISTRY_ONLY=1`; see [Nested-group repos](#nested-group-repos-gitlab-subgroups-behind-the-proxy) |
| HTTP 404 on every ref of an existing GitLab project | Default branch is `master`, APM defaults to `main` | Pin the ref: `<repo>#master` in `apm.yml` |
| Upstream URL in `X-Artifactory-Origin-Remote-Path` has a duplicated group name | The remote's "Repository Path Prefix" is prepending a segment that's also in the request | Clear the prefix field on the remote |

For fully disconnected CI (no proxy reach at all), build a bundle on a
connected host with `apm pack` and restore offline. See
[Pack and distribute](../producer/pack-a-bundle/).

## See also

- [Authentication](../consumer/authentication/) -- token resolution order
- [Private and org packages](../consumer/private-and-org-packages/) -- per-host PAT scoping
- [Pack and distribute](../producer/pack-a-bundle/) -- air-gapped bundle delivery
- [Governance overview](./governance-overview/) -- policy-cache offline story
