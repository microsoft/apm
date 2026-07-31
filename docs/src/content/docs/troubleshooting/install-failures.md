---
title: "Install failures"
description: "Diagnose and recover from apm install failures: auth, network, lockfile, cache, and partial installs."
sidebar:
  order: 2
---

When `apm install` fails, work through these sections in order. Most failures fall into one of five buckets: auth, network/TLS, lockfile, cache, or a partial install left behind by a previous crash.

For TLS-specific failures see [SSL / TLS issues](../ssl-issues/). For the full flag reference see [`apm install`](../../reference/cli/install/). For env-var precedence see [Environment variables](../../reference/environment-variables/).

## 1. Authentication failures

Symptom: `403`, `401`, "authentication required", or "Repository not found" on a repo you can clone manually with `git`.

### GitHub precedence chain

APM resolves a token per host class. For `github.com`, GHE Cloud, and GHES the order is:

```text
GITHUB_APM_PAT_<ORG>   ->   GITHUB_APM_PAT   ->   GITHUB_TOKEN   ->   GH_TOKEN   ->   gh auth token   ->   git credential helper
```

`<ORG>` is the package owner uppercased with non-alphanumeric chars replaced by `_` (so `my-org/pkg` -> `GITHUB_APM_PAT_MY_ORG`). Per-org PATs win over the global `GITHUB_APM_PAT`. See [Environment variables](../../reference/environment-variables/) for the full table.

Common fixes:

- [+] Set a fine-grained PAT with `Contents: Read` on the org/repo:
  ```bash
  export GITHUB_APM_PAT=ghp_xxx
  ```
- [+] For multi-org installs, scope per org:
  ```bash
  export GITHUB_APM_PAT_ACME=ghp_acme_xxx
  export GITHUB_APM_PAT_CONTOSO=ghp_contoso_xxx
  ```
- [!] `gh auth token` is consulted only after the env vars. If the wrong identity is logged in via `gh`, set `GITHUB_APM_PAT` explicitly to override.

### GHES (GitHub Enterprise Server)

Set `GITHUB_HOST` for a GHES instance:

```bash
export GITHUB_HOST=ghe.example.com
export GITHUB_APM_PAT=ghp_ghes_xxx
```

APM applies the GitHub precedence chain and composes API and clone URLs for
that host. Azure DevOps Server uses the separate host configuration below.

### GitLab (SaaS and self-managed)

```text
GITLAB_APM_PAT   ->   GITLAB_TOKEN   ->   git credential helper
```

For self-managed:

```bash
export GITLAB_HOST=gitlab.example.com
export GITLAB_APM_PAT=glpat-xxx
```

If you operate multiple GitLab instances, list them in `APM_GITLAB_HOSTS` (comma-separated) so APM classifies them as GitLab-class.

### Azure DevOps

Azure DevOps Services hosts (`dev.azure.com` and `*.visualstudio.com`) use:

```text
ADO_APM_PAT   ->   AAD bearer (via az cli)   ->   none
```

```bash
export ADO_APM_PAT=ado_pat_xxx
```

If `ADO_APM_PAT` is set and `az` is logged in but the request still fails,
APM emits a hint that the PAT was rejected and the bearer was tried. Unset
the PAT to test bearer auth alone:

```bash
unset ADO_APM_PAT
az login
apm install
```

Azure DevOps Server is PAT-only:

```text
ADO_APM_PAT   ->   none
```

```bash
export ADO_HOST=ado.example.com
export ADO_APM_PAT=ado_pat_xxx
```

Use `APM_ADO_HOSTS` for multiple Server instances. A configured ADO host wins
when `GITHUB_HOST` names the same host. Host variables accept FQDNs only,
without a scheme, port, or path; put a non-default HTTPS port in the
dependency URL. Use a root-hosted collection path. APM rejects `/tfs/`
prefixes. If the PAT is rejected, rotate it -- Azure CLI bearer auth does
not apply to Server.

### Test your token

Bypass APM and probe the host directly:

```bash
# GitHub / GHE / GHES
curl -H "Authorization: Bearer $GITHUB_APM_PAT" \
     "https://${GITHUB_HOST:-api.github.com}/repos/<owner>/<repo>"

# GitLab
curl -H "PRIVATE-TOKEN: $GITLAB_APM_PAT" \
     "https://${GITLAB_HOST:-gitlab.com}/api/v4/projects/<owner>%2F<repo>"

# Azure DevOps Services (PAT)
curl -u ":${ADO_APM_PAT}" \
     "https://dev.azure.com/<org>/<project>/_apis/git/repositories/<repo>?api-version=7.0"

# Azure DevOps Server (remove :8443 when using default HTTPS)
curl -u ":${ADO_APM_PAT}" \
     "https://${ADO_HOST}:8443/DefaultCollection/<project>/_apis/git/repositories/<repo>?api-version=7.0"
```

A `200` confirms the credential works. If `apm install` still fails, run
`apm install --verbose` to see which source APM picked. For Server, confirm
the hostname is registered through `ADO_HOST` or `APM_ADO_HOSTS`.

For end-to-end auth setup see [Authentication](../../getting-started/authentication/).

## 2. Network and TLS

### TLS verification

```text
[!] TLS verification failed
```

APM verifies HTTPS against the OS trust store by default. Behind a corporate proxy, install your org's CA into the OS trust store; for a per-shell override, set `REQUESTS_CA_BUNDLE` to a readable PEM bundle. Full walkthrough: [SSL / TLS issues](../ssl-issues/).

### Timeouts and proxies

APM honours the standard `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` env vars. If clones hang:

- [>] Confirm `git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=10 ls-remote <url>` succeeds.
- [>] Set `GIT_SSH_COMMAND` to add verbose diagnostics for SSH transports:
  ```bash
  export GIT_SSH_COMMAND="ssh -vvv"
  ```
  APM preserves your value when composing its own SSH env.

### Air-gapped and proxied registries

Route all package downloads through an enterprise proxy:

```bash
export PROXY_REGISTRY_URL=https://artifactory.example.com/apm
export PROXY_REGISTRY_TOKEN=xxx
```

See [Registry proxy](../../enterprise/registry-proxy/) for setup, including `PROXY_REGISTRY_ALLOW_HTTP` for development environments.

## 3. Lockfile mismatches

### Manifest changed but lockfile didn't

If you edited `apm.yml` but `apm.lock.yaml` still pins the old refs, run a plain install to regenerate the lockfile:

```bash
apm install
```

This re-resolves and rewrites `apm.lock.yaml`. Commit the result.

### Drifted refs

To force re-resolution to the latest version or Git ref allowed by `apm.yml`:

```bash
apm install --update
```

This is the only flag that will move pins forward; a bare `apm install` keeps existing pins where they are still satisfiable.

### Detecting drift in CI

The CI gate compares the deployed tree against what the lockfile says should be there:

```bash
apm audit --ci
```

A non-zero exit means the working tree has diverged from `apm.lock.yaml` -- either re-run `apm install` to restore parity, or commit the new lockfile if the drift was intentional.

For the full flag list see [`apm install`](../../reference/cli/install/).

## 4. Cache problems

APM uses a content-addressed cache for git clones and HTTP downloads. Corrupt or stale entries usually surface as checksum mismatches or "object not found" errors mid-install.

### Diagnose

```bash
apm cache info
```

Reports the cache root, git-repo count, checkout count, HTTP entry count, and total size on disk.

### Recover

Bypass the cache and re-resolve refs for a single run:

```bash
apm install --refresh
```

This re-fetches every dependency from upstream and rewrites cache entries.

Disable the cache entirely (read and write) for one invocation:

```bash
APM_NO_CACHE=1 apm install
```

Wipe the cache when entries are demonstrably corrupt:

```bash
apm cache clean
```

Or drop only stale entries:

```bash
apm cache prune --days 30
```

See [`apm cache`](../../reference/cli/cache/) for the full subcommand reference.

## 5. Partial install recovery

`apm install` is designed to be re-run safely. If a previous invocation died mid-flight (Ctrl-C, OOM, network drop), just run it again:

```bash
apm install
```

The cache short-circuits already-downloaded packages and the integrate phase overwrites partially-deployed files.

If resolution rejects a cyclic dependency graph, fix the package manifests and run `apm install` again. APM rolls back only the package snapshots staged by the rejected resolution, so no manual `apm_modules/` deletion is required.

If files in `apm_modules/` or under target harness directories look corrupt, force a fresh deploy by combining cache bypass with overwrite:

```bash
apm install --refresh --force
```

`--force` overwrites locally-authored files on collision **and** bypasses the security scan's critical-finding block -- use it only after you've inspected the diff. See [`apm install`](../../reference/cli/install/#policy-and-trust).

To wipe everything and start clean:

```bash
rm -rf apm_modules/
apm cache clean
apm install
```

## 6. Verbose diagnostics

When the above doesn't pinpoint the failure, raise the noise floor:

```bash
apm install --verbose
```

Shows per-file paths, the auth source picked for each host, cache hits and misses, and full error context in the diagnostic summary.

For low-level download, file-op, and clone-cache traces:

```bash
APM_DEBUG=1 apm install --verbose
```

Combine the two to capture the maximum signal in a single run. Pipe to a file before sharing:

```bash
APM_DEBUG=1 apm install --verbose 2>&1 | tee install.log
```

If you file an issue, attach `install.log`, the relevant `apm.yml` and `apm.lock.yaml`, and the output of `apm cache info`.
