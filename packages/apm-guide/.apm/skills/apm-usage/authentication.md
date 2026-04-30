# Authentication

## Token precedence chain

APM checks these sources in order, using the first valid token found:

| Priority | Variable | Scope | Notes |
|----------|----------|-------|-------|
| 1 | `GITHUB_APM_PAT_{ORG}` | Per-org | Org name uppercased, hyphens to underscores |
| 2 | `GITHUB_APM_PAT` | Global | Falls back to git credential if rejected |
| 3 | `GITHUB_TOKEN` | Global | Shared with GitHub Actions |
| 4 | `GH_TOKEN` | Global | Set by `gh auth login` |
| 5 | `git credential fill` | Per-host | System credential manager |
| -- | None | -- | Unauthenticated (public GitHub repos only) |

## Per-org setup

Use per-org tokens when accessing packages across multiple organizations:

```bash
export GITHUB_APM_PAT_CONTOSO=ghp_token_for_contoso
export GITHUB_APM_PAT_FABRIKAM=ghp_token_for_fabrikam
```

**Naming rules:**
- Uppercase the org name
- Replace hyphens with underscores
- Example: `contoso-microsoft` -> `GITHUB_APM_PAT_CONTOSO_MICROSOFT`

## Fine-grained PAT requirements

Required permissions:
- **Metadata:** Read
- **Contents:** Read
- **Repository access:** All repos or specific repos

**Important:** The resource owner must be the **organization**, not your user
account. User-scoped fine-grained PATs cannot access org repos even if you are
a member.

For SSO-protected orgs, authorize the token under Settings > Tokens > Configure SSO.

## Azure DevOps (ADO)

ADO supports two auth modes; the GitHub token chain does not apply. The recommended
approach is `az login`; explicit PATs are also supported. Resolution order:

1. `ADO_APM_PAT` env var if set
2. AAD bearer from `az account get-access-token` if `az` is installed and signed in
3. Otherwise: auth-failed error with actionable diagnostic

```bash
# Recommended: bearer mode (no env var needed)
az login --tenant <tenant-id>
apm install dev.azure.com/org/project/_git/repo

# Alternative: PAT mode
export ADO_APM_PAT=your_ado_pat
apm install dev.azure.com/org/project/_git/repo
```

ADO paths use the 3-segment format: `org/project/repo`. Auth is always required.

**Finding your tenant ID:** visit `https://dev.azure.com/{org}/_settings/organizationAad`,
or run `az login` and inspect `az account show --query tenantId -o tsv`.

If `ADO_APM_PAT` is set but ADO returns 401, APM silently retries with the `az` bearer and warns:
`[!] ADO_APM_PAT was rejected for {host} (HTTP 401); fell back to az cli bearer.`

When auth fails entirely, APM prints a targeted diagnostic (not a generic "not accessible"
message). For `--update` operations, a pre-flight auth check runs before any files are
modified -- on failure you see `No files were modified`.

### ADO auth troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No ADO_APM_PAT was set and az CLI is not installed` | Neither path available | Install `az` from https://aka.ms/installazurecli and run `az login --tenant <tenant>`, or set `ADO_APM_PAT` |
| `az CLI is installed but no active session was found` | `az account show` fails | Run `az login --tenant <tenant>` against the tenant that owns the org |
| `az CLI returned a token but the org does not accept it (likely a tenant mismatch)` | Wrong tenant | Run `az login --tenant <correct-tenant>`, or set `ADO_APM_PAT` |
| `ADO_APM_PAT was rejected (HTTP 401) and no az cli fallback was available` | Stale PAT, no `az` | Rotate the PAT, or install `az` and run `az login --tenant <tenant>` |

## GitHub Enterprise Server (GHES)

```bash
export GITHUB_HOST=github.company.com
export GITHUB_APM_PAT_MYORG=ghp_ghes_token
apm install myorg/internal-package       # resolves to github.company.com
apm pack                                 # marketplace.json also resolves against github.company.com
```

## GHE Cloud data residency (*.ghe.com)

```bash
export GITHUB_APM_PAT_MYENTERPRISE=ghp_enterprise_token
apm install myenterprise.ghe.com/platform/standards
```

No public repos exist on `*.ghe.com` -- auth is always required.

## Enterprise Managed Users (EMU)

- EMU orgs live on `github.com` (e.g., `contoso-microsoft`) or `*.ghe.com`
- Use standard PAT prefixes (`ghp_`, `github_pat_`)
- Fine-grained PATs must use the EMU org as resource owner
- EMU accounts cannot access public repos on github.com
- If mixing enterprise and public repos, use separate tokens

## Artifactory proxy (air-gapped environments)

```bash
export PROXY_REGISTRY_URL=https://artifactory.company.com/apm-remote
export PROXY_REGISTRY_TOKEN=your_bearer_token
export PROXY_REGISTRY_ONLY=1                   # optional: proxy-only mode
```

When `PROXY_REGISTRY_ONLY=1`, APM routes all traffic through the proxy and
never contacts GitHub directly.

## Troubleshooting

```bash
# Diagnose the auth chain -- shows which token source is used
apm install --verbose your-org/package

# Increase git credential timeout (default 30s, max 180s)
export APM_GIT_CREDENTIAL_TIMEOUT=120
```

### Custom-port hosts and per-port credentials

Self-hosted Git instances on non-standard ports (e.g. Bitbucket Datacenter
on port 7999) are now first-class. APM sends `host=<host>:<port>` to
`git credential fill` per the [`gitcredentials(7)`](https://git-scm.com/docs/gitcredentials)
protocol; the credential cache and token resolution are also keyed by
`(host, port)` so distinct PATs on the same hostname do not collide.

Whether the helper actually returns per-port credentials depends on the
backend:

| Helper | Honors port-in-host? |
|---|---|
| git-credential-manager (GCM) | Yes |
| macOS Keychain (`osxkeychain`) | Yes (stores full `host:port` as key) |
| `libsecret` (Linux) | Yes (port in URI) |
| `gh auth git-credential` | No -- but only used for GitHub hosts, which do not use custom ports |

If APM resolves the wrong credential for a custom-port host, confirm your
helper keys by `host:port`; otherwise either switch helpers or store the
credential under a fully qualified `https://<host>:<port>/` URL.

### SSH connection hangs on corporate/VPN networks

APM tries SSH as a fallback when HTTPS auth is not available. On networks
that silently drop SSH traffic (port 22), this can appear to hang. APM sets
`GIT_SSH_COMMAND="ssh -o ConnectTimeout=30"` so SSH attempts fail within
30 seconds and the fallback chain continues to plain HTTPS with git
credential helpers.

To override the SSH command (e.g., custom key path), set `GIT_SSH_COMMAND`
in your environment. APM appends `-o ConnectTimeout=30` unless it finds
`ConnectTimeout` already present in your value.
