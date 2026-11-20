---
title: Install through a proxy or offline
description: Route APM downloads through a corporate proxy or a registry mirror, trust a corporate CA, and install from a bundle on machines with no network.
---

This page is for a platform team whose machines cannot reach GitHub directly, or cannot
reach any network at all. Pick the row that matches your network, then follow its
section. All options compose.

| Your network | Use |
|---|---|
| Outbound traffic goes through a forward proxy | `HTTPS_PROXY` and `NO_PROXY` |
| A TLS-inspecting proxy re-signs HTTPS with a corporate CA | `APM_EXTRA_CA_BUNDLE` |
| Every package download must go through a mirror such as JFrog Artifactory | `PROXY_REGISTRY_URL` and `PROXY_REGISTRY_ONLY=1` |
| No network at all | A bundle built with `apm pack`, installed with `apm install ./bundle.zip` |

## Use a forward proxy and a corporate CA

APM downloads with `git` and Python HTTPS. Both honor the standard variables:

```bash frame="terminal"
export HTTPS_PROXY=http://proxy.corp.example.com:8080
export NO_PROXY=localhost,127.0.0.1,.corp.example.com
```

If `git clone` of your private repositories works through the proxy, `apm install`
works too. When the proxy re-signs TLS, add your CA without dropping the public roots:

```bash frame="terminal"
export APM_EXTRA_CA_BUNDLE=/etc/ssl/corp-ca.pem
```

The file must hold PEM certificates and no private keys. Git needs the CA in its own trust store. See
[SSL / TLS issues](../../troubleshooting/ssl-issues/).

## Route downloads through a registry proxy

A registry proxy is a mirror, such as a JFrog Artifactory remote repository, that fronts
GitHub or GitLab so every package archive passes through infrastructure you log and
control. Set it per machine or per CI job:

```bash frame="terminal"
export PROXY_REGISTRY_URL=https://art.example.com/artifactory/github
export PROXY_REGISTRY_TOKEN=<token>
export PROXY_REGISTRY_ONLY=1
```

| Variable | Effect |
|---|---|
| `PROXY_REGISTRY_URL` | Base URL of the proxy. APM downloads package archives from it. |
| `PROXY_REGISTRY_TOKEN` | Bearer token, never sent to hosts other than the proxy. |
| `PROXY_REGISTRY_ONLY` | `1` refuses any direct download from the upstream host, including on lockfile replay, and skips source-host probes such as the `apm update` auth check. |
| `PROXY_REGISTRY_ALLOW_HTTP` | `1` authorizes sending `PROXY_REGISTRY_TOKEN` over `http://` to a host that is not loopback or a private network address (10/8, 172.16/12, 192.168/16). Without it, the token is not sent there and a `[!]` line says why. |

The lockfile records which entries came through the proxy. With `PROXY_REGISTRY_ONLY=1`,
a lockfile written without the proxy is refused before anything downloads:

```text frame="none"
PROXY_REGISTRY_ONLY is set but the lockfile contains dependencies locked to direct VCS sources:
  - microsoft/apm-sample-package (host: github.com)
  - github/awesome-copilot/skills/review-and-refactor (host: github.com)
Run 'apm update' to re-resolve through the registry, or unset PROXY_REGISTRY_ONLY.
[x] Install failed after 0.3s.
```

Run `apm update` once through the proxy and commit the new `apm.lock.yaml`. That update
records what the proxy serves at that moment, so review the lockfile diff like any other.
From then on, archives the proxy serves are checked against the locked content hashes,
so a proxy that alters a package later is caught.

**What the proxy does not carry.** The `apm-policy.yml` fetch always goes direct: it is
the governance channel, and APM prints one `[i]` line naming its origin. An explicit
`apm self-update` also goes direct; the automatic update check is skipped (`apm -v` notes it). With `PROXY_REGISTRY_ONLY=1`, APM refuses the other routes that
would bypass the proxy: Azure DevOps dependencies, lookups in the default MCP registry
(point `MCP_REGISTRY_URL` or `apm config set mcp-registry-url` at a proxied registry to
allow them), and marketplace files from Azure DevOps, generic git or URL sources. Without
`PROXY_REGISTRY_URL`, it refuses every marketplace. GitHub marketplace files go through the
proxy.

**Version ranges need a listing.** APM downloads an exact ref through the proxy but does
not list tags through it. With `PROXY_REGISTRY_ONLY=1`, a range such as `^1.2.0` fails with
`Cannot resolve the range`, and `apm outdated` reports `unknown`. Pin exact tags the
way the upstream repository names them (`#v1.4.0`), and commit the lockfile; locked
ranges replay without a listing. A server that implements the
[Registry HTTP API](../../reference/registry-http-api/) (Experimental) can list versions.

### Nested-group repos (GitLab subgroups behind the proxy)

GitLab projects can sit at any depth (`group/subgroup/project`). With
`PROXY_REGISTRY_ONLY=1`, APM treats every segment as part of the repository path, then
probes the proxy to find where the repository ends and a sub-path such as
`skills/<name>` begins:

```yaml title="apm.yml"
dependencies:
  apm:
    - group/subgroup/project#v1.2.0
    - group/subgroup/project/skills/review
```

Set `PROXY_REGISTRY_ONLY` in the same place you pin the APM version: the same shorthand parses
differently without it. Configure the proxy's GitLab remote with a token that has
`read_repository` scope, and pin refs on projects whose default branch is not `main`.

## Install with no network

Build a bundle on a connected machine, carry it across, and install it.

1. On the connected machine, in a repository whose dependencies are installed and
   locked:

   ```bash frame="terminal"
   apm pack --archive
   ```

   APM writes `build/<name>-<version>.zip` with the deployed files and an
   `apm.lock.yaml` that lists a SHA-256 hash for every file in the bundle.

2. On the disconnected machine:

   ```bash frame="terminal"
   apm install ./build/my-project-1.0.0.zip
   ```

   APM checks every file against the bundle's hashes and rejects symlinks, unlisted
   files and mismatches before deploying. The hashes travel inside the bundle, so they
   catch corruption, not a bundle someone rebuilt: carry it over a channel you trust. `apm install` does not edit `apm.yml`; it records the
   deployed files in `apm.lock.yaml`. Org policy resolves as for a package install; on
   a disconnected machine the fetch fails and `policy.fetch_failure_default` decides.
   `security.integrity.require_hashes: true` refuses bundles without hashes.

To enforce org policy on a disconnected CI runner, vendor the policy file and pass it
explicitly. An explicit source that fails to load fails the check:

```bash frame="terminal"
apm audit --ci --policy ./vendor/apm-policy.yml
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured` | Set `PROXY_REGISTRY_URL`, or the dependency is on a host the proxy does not carry |
| HTTP 401 or 403 from the proxy | Check `PROXY_REGISTRY_TOKEN` has read access to the upstream path |
| `Invalid zip archive`, body starts with `<!DOCTYPE html>` | The upstream returned a sign-in page and the proxy cached it. Fix the remote's credentials and purge its cache |
| TLS verification failed | Set `APM_EXTRA_CA_BUNDLE`; see [SSL / TLS issues](../../troubleshooting/ssl-issues/) |

The legacy names `ARTIFACTORY_BASE_URL`, `ARTIFACTORY_APM_TOKEN` and `ARTIFACTORY_ONLY`
still work and warn; rename them. All variables:
[Environment variables](../../reference/environment-variables/).
