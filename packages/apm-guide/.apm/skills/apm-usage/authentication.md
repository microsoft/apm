# Authentication

## Token precedence chain

APM checks these sources in order, using the first valid token found:

| Priority | Variable | Scope | Notes |
|----------|----------|-------|-------|
| 1 | `GITHUB_APM_PAT_{ORG}` | Per-org | Org name uppercased, hyphens to underscores |
| 2 | `GITHUB_APM_PAT` | Global | Falls back to git credential if rejected |
| 3 | `GITHUB_TOKEN` | Global | Shared with GitHub Actions |
| 4 | `GH_TOKEN` | Global | Set by `gh auth login` |
| 5 | `gh auth token --hostname <host>` | GitHub-like hosts | Active `gh auth login` account |
| 6 | `git credential fill` | Per-host | System credential manager. APM forwards `path=<owner>/<repo>` so Git Credential Manager users with `credential.useHttpPath = true` get per-URL account selection (no account-picker prompt). |
| -- | None | -- | Unauthenticated (public GitHub repos only) |

APM checks the active `gh` CLI account before invoking OS credential helpers. This reduces ambiguous multi-account prompts on hosts like github.com. If the `gh` CLI is not installed or no account is active, APM skips this step silently and continues to `git credential fill`.

For multi-account Git Credential Manager setups, see the [Multi-account Git Credential Manager](https://microsoft.github.io/apm/getting-started/authentication/#multi-account-git-credential-manager) section in the main authentication guide.

## Marketplace transport

For in-repository plugins from GitLab and generic git marketplaces, an SSH
registration stays SSH when APM generates the concrete `git:` and `path:`
dependency. Existing SSH keys keep working instead of the dependency being
rewritten to HTTPS.

## GitLab hosts

`gitlab.com` is detected automatically. For self-managed GitLab, set
`GITLAB_HOST`, list multiple hosts in `APM_GITLAB_HOSTS`, or mark a single
object-form dependency with `type: gitlab`:

```yaml
- git: https://code.acme.com/platform/standards.git
  type: gitlab
```

GitLab credentials use `GITLAB_APM_PAT`, then `GITLAB_TOKEN`, then host
credentials. GitHub PAT variables are not used for GitLab-class hosts. See the
main [authentication guide](https://microsoft.github.io/apm/getting-started/authentication/)
for the full host-class precedence rules.

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

## GitLab (SaaS or self-managed)

APM fetches `path:`-specified files from GitLab dependencies via git sparse/partial
checkout (the same transport as the clone). Git transport is tried first, so SSH
keys and git credential helpers work without any extra token, and self-hosted
GitLab instances where the API returns 410 (disabled) no longer fail. Explicit
`git:` / SSH URLs carry the host in the dependency; set `GITLAB_HOST` (or
`APM_GITLAB_HOSTS`) only when bare-host or shorthand forms should classify as
GitLab.

If git transport is unavailable, `GITLAB_APM_PAT` is the fallback:

```bash
export GITLAB_APM_PAT=glpat_your_token
apm install
```

`GITLAB_TOKEN` is accepted as a lower-precedence fallback. `git credential fill` is
also tried (same as for GitHub) so credential-manager users need no env var at all.

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

## Registry tokens (experimental)

REST-based APM registries (behind `apm experimental enable registries`) use
a **separate** credential chain from the GitHub / ADO token chains above.
Tokens are scoped per registry name as declared in `apm.yml`'s `registries:`
block (or in `~/.apm/config.json`).

**Env-var naming:** `APM_REGISTRY_TOKEN_{NAME}` where `{NAME}` is the
registry name uppercased, with `-` and `.` mapped to `_`.

| Registry name | Env var |
|---------------|---------|
| `jf-skills` | `APM_REGISTRY_TOKEN_JF_SKILLS` |
| `corp-main` | `APM_REGISTRY_TOKEN_CORP_MAIN` |
| `corp.snapshots` | `APM_REGISTRY_TOKEN_CORP_SNAPSHOTS` |

**Auth modes:**

| Env var(s) | Sent as |
|------------|---------|
| `APM_REGISTRY_TOKEN_{NAME}` | `Authorization: Bearer <token>` |
| `APM_REGISTRY_USER_{NAME}` + `APM_REGISTRY_PASS_{NAME}` | `Authorization: Basic <base64(user:pass)>` |

Bearer wins when both forms are set.

**Token precedence (per registry, highest wins):**

1. `APM_REGISTRY_TOKEN_{NAME}` (or `APM_REGISTRY_USER_{NAME}` + `APM_REGISTRY_PASS_{NAME}`) env var
2. `registry.<name>.token` in `~/.apm/config.json` (via `apm config set`)
3. Unauthenticated -- APM sends the request anonymously first; remediation
   pointing at `APM_REGISTRY_TOKEN_<NAME>` is printed only on `401`/`403`

```bash
# Bearer token for registry "jf-skills"
export APM_REGISTRY_TOKEN_JF_SKILLS=eyJ...

# Or HTTP Basic
export APM_REGISTRY_USER_JF_SKILLS=alice@example.com
export APM_REGISTRY_PASS_JF_SKILLS=secret

# Or stored in user config (never committed)
apm config set registry.jf-skills.token eyJ...
```

**Relationship to other chains:** `APM_REGISTRY_*` is a distinct prefix
from `GITHUB_APM_PAT_*`, `ADO_APM_PAT`, `PROXY_REGISTRY_*`, and
`ARTIFACTORY_APM_TOKEN`. There is no collision: registry-routed deps go
through the registry chain only; Git-routed deps continue through the
GitHub / ADO chains above. A single project with both registry and Git
deps uses both chains side-by-side.

**Sanitization trap:** distinct registry names can collapse to the same
env var (`corp-main`, `corp.main`, `Corp-Main` all sanitize to
`APM_REGISTRY_TOKEN_CORP_MAIN`). Do not declare two registries whose
names sanitize identically. Prefer hyphenated lowercase names.

Tokens MUST NOT appear in repo YAML. In `apm.yml`, a `token:` field
under a `registries:` entry is rejected at parse time (token trap). In
`apm-policy.yml`, a top-level `token:` key is not a recognized policy
field and surfaces as an "Unknown top-level policy key" warning rather
than a hard parse error -- but storing tokens there is still
unsupported and committing one would leak the secret into the repo.
Store tokens in env vars or `~/.apm/config.json` only.

## External scanner LLM keys (experimental)

When LLM-powered analysis is enabled for an external SARIF scanner (`apm
audit --external <name> --external-llm`, behind `apm experimental enable
external-scanners`), the scanner reads its own API key from your
environment -- `OPENAI_API_KEY` or `NVIDIA_INFERENCE_KEY`. APM **never
stores, prompts for, or persists** these keys; they come straight from
your shell environment and are forwarded to the scanner subprocess only
when LLM mode is active for that run, then stripped otherwise. If
`--external-llm` is set and no key is present, the scan fails closed with
an actionable error. Scanner stderr is secret-redacted before APM surfaces
it in any error or log. Do not pass keys as scanner flags (`--external-args`
rejects secret-looking flags) -- export them as env vars instead.

## Install validation chain

`apm install <package>` validates a virtual subdirectory package (`owner/repo/path#ref`) before writing it to `apm.yml`. The chain mirrors the actual clone auth path so a credential that succeeds for `git clone` is never false-rejected by the installer:

1. **Marker-file probes** via raw content -- `apm.yml`, `SKILL.md`, `plugin.json`, `README.md`. Fast positive signal; absence is not a failure.
2. **Contents API directory probe** -- `GET /repos/{owner}/{repo}/contents/{path}?ref={ref}`. Confirms the directory exists at the ref.
3. **`git ls-remote`** with the install auth chain (PAT header-injected, then plain HTTPS w/ credential helper, then SSH if `--ssh` or `--allow-protocol-fallback`). Confirms the ref exists.
4. **Shallow `git fetch --depth=1 --filter=tree:0` + `git ls-tree`** at the resolved ref -- the path probe that confirms the subdirectory exists at that ref. Required to close the fail-open hole where step 3 would otherwise pass any successful repo handshake.

Steps 3 and 4 only run for explicit `#ref` pins (not for unpinned default-branch deps), and only when the API steps fail. Azure DevOps tokens (PAT or AAD bearer) are injected via `http.extraheader` (`Authorization: Bearer ...`) and never embedded in the clone URL.

**Yellow signal:** when steps 1-2 fail and steps 3-4 succeed, APM emits a stderr warning -- `[!] API validation skipped for {pkg}; resolved via git credential fallback.` This is security-relevant: a scoped fine-grained PAT may have *correctly* rejected a package on the API surface and the broader git credential chain accepted it. Operators should be able to see that signal in default CI logs.

**Terminal error** when all four steps fail: `[x] all probes failed (marker-file, Contents API, git ls-remote, shallow-fetch) -- verify the path and ref exist and that your credentials have read access (run with --verbose for the full probe log)`.

```bash
# See the full probe log when validation fails
apm install --verbose owner/repo/path#v1.2.0
```

## Troubleshooting

```bash
# Diagnose the auth chain -- shows which token source is used
apm install --verbose your-org/package

# Increase git credential timeout (default 60s, max 180s)
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

To verify what your helper returns for a custom-port host, use the
helper-agnostic command APM itself calls:

```sh
printf 'protocol=https\nhost=<host>:<port>\n\n' | git credential fill
```

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
