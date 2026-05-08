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

ADO hosts (`dev.azure.com`, `*.visualstudio.com`) resolve auth in this order:

1. `ADO_APM_PAT` env var if set
2. AAD bearer via `az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798` if `az` is installed and `az account show` succeeds
3. Otherwise: auth-failed error from `build_error_context`

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

**Diagnostic cases** (`_emit_stale_pat_diagnostic` + `build_error_context` in `src/apm_cli/core/auth.py`):

1. No PAT, no `az`: `No ADO_APM_PAT was set and az CLI is not installed.` -> install `az`, run `az login --tenant <tenant>`, or set `ADO_APM_PAT`.
2. No PAT, `az` not signed in: `az CLI is installed but no active session was found.` -> run `az login --tenant <tenant>` against the tenant that owns the org, or set `ADO_APM_PAT`.
3. No PAT, wrong tenant: `az CLI returned a token but the org does not accept it (likely a tenant mismatch).` -> run `az login --tenant <correct-tenant>`, or set `ADO_APM_PAT`.
4. PAT 401, no `az` fallback: `ADO_APM_PAT was rejected (HTTP 401) and no az cli fallback was available.` -> rotate the PAT, or install `az` and run `az login --tenant <tenant>`.
