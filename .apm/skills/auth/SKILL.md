---
name: auth
description: >
  Activate when code touches token management, credential resolution, git auth
  flows, GITHUB_APM_PAT, ADO_APM_PAT, AuthResolver, HostInfo, AuthContext, or
  any remote host authentication -- even if 'auth' isn't mentioned explicitly.
---

# Auth Skill

[Auth expert persona](../../agents/auth-expert.agent.md)

## When to activate

- Any change to `src/apm_cli/core/auth.py` or `src/apm_cli/core/token_manager.py`
- Code that reads `GITHUB_APM_PAT`, `GITHUB_TOKEN`, `GH_TOKEN`, `ADO_APM_PAT`
- Code using `git ls-remote`, `git clone`, or GitHub/ADO API calls
- Error messages mentioning tokens, authentication, or credentials
- Changes to `github_downloader.py` auth paths
- Per-host or per-org token resolution logic

## Key rule

All auth flows MUST go through `AuthResolver`. No direct `os.getenv()` for token variables in application code.

## Canonical reference

The full per-org -> global -> credential-fill -> fallback resolution flow is in [`docs/src/content/docs/getting-started/authentication.md`](../../../docs/src/content/docs/getting-started/authentication.md) (mermaid flowchart). Treat it as the single source of truth; if behavior diverges, fix the diagram in the same PR.

## Bearer-token authentication for ADO

Azure DevOps Services (`dev.azure.com`, `*.visualstudio.com`) resolve auth in this order:

1. `ADO_APM_PAT` env var if set
2. AAD bearer via `az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798` if `az` is installed and `az account show` succeeds
3. Path-scoped `git credential fill` (Git Credential Manager) after an auth-failure signal (`is_ado_auth_failure_signal`)
4. Otherwise: `AdoAuthChainExhaustedError` from `try_with_fallback`, or `build_error_context` on resolve-time failures

Azure DevOps Server hosts (`ADO_HOST` / `APM_ADO_HOSTS`) skip az bearer: PAT, then fill.

`ADO_APM_PAT` is the env var name used by the auth flow. The AAD bearer source constant lives in `src/apm_cli/core/token_manager.py` as `GitHubTokenManager.ADO_BEARER_SOURCE = "AAD_BEARER_AZ_CLI"`.

**Stale-PAT silent fallback:** if `ADO_APM_PAT` is rejected with HTTP 401, APM retries with the az bearer and emits:

```
[!] ADO_APM_PAT was rejected for {host} (HTTP 401); fell back to az cli bearer.
[!]     Consider unsetting the stale variable.
```

**Verbose source line** (one per host, emitted under `--verbose`):

```
[i] dev.azure.com -- using bearer from az cli (source: AAD_BEARER_AZ_CLI)
[i] dev.azure.com -- token from ADO_APM_PAT
```

**Diagnostic cases** (`build_error_context` + `AdoAuthChainExhaustedError` in `src/apm_cli/core/auth.py`):

1. No PAT, no `az`: `Azure DevOps requires authentication. You have two options` -> install `az` and `az login`, set `ADO_APM_PAT`, or store a Git Credential Manager credential.
2. No PAT, `az` not signed in: same two-options copy with `az login` first.
3. No PAT, wrong tenant: `Your az cli session (tenant: ...) returned a bearer token, but Azure DevOps rejected it (HTTP 401).` -> `az login --tenant <correct-tenant>`, or set `ADO_APM_PAT`.
4. PAT set, request failed: `ADO_APM_PAT is set, but the Azure DevOps request failed.` -> rotate the PAT, `az login` on Services, or store a GCM credential.
5. Fill hop exhausted: `Authentication failed for {host}: ... git credential fill was rejected.` (`AdoAuthChainExhaustedError`) -> refresh PAT, `az login` on Services, or store a GCM credential. Non-auth failures (DNS, TLS, timeout) re-raise; do not wrap them as chain exhaustion.
6. Server, no PAT: `Azure DevOps Server requires ADO_APM_PAT or a Git credential helper.` -> set `ADO_APM_PAT` or store a GCM credential (`az` does not apply).
