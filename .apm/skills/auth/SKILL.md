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

Azure DevOps Services (`dev.azure.com`, `*.visualstudio.com`) use this chain:

1. `ADO_APM_PAT` env var if set
2. AAD bearer via `az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798` if `az` is installed and `az account show` succeeds
3. Operation-time path-scoped `git credential fill` (`org/project/_git/repo`, sent as Basic `username:password`, cached per repository) after an auth failure classified by `classify_auth_outcome` (`src/apm_cli/core/auth_outcome.py`): HTTP 401/403, a redirect to the ADO sign-in service, an HTML sign-in page, or a Git auth error
4. Otherwise: `AdoAuthChainExhaustedError` (with its `AuthAttemptLog`) from `try_with_fallback`, or `build_error_context` on resolve-time failures

`AuthResolver.try_with_fallback` is the only chain entry; the chain lives in `src/apm_cli/core/auth_chain.py`. Call sites raise `AuthAttemptFailed` with the raw response or process result and never classify auth failures themselves (guard `transport-platform-auth-outcome-owner`). ADO HTTP requests pass `auth=AttemptAuth(git_env)` or `allow_netrc=False` so only the chain-selected credential is sent. APM never calls `git credential approve`/`reject`; fill runs with `GCM_INTERACTIVE=0` when stdin is not a TTY or `CI` is set.

Azure DevOps Server hosts (`ADO_HOST` / `APM_ADO_HOSTS`) skip az bearer: PAT, then fill.

`ADO_APM_PAT` is the env var name used by the auth flow. The AAD bearer source constant lives in `src/apm_cli/core/token_manager.py` as `GitHubTokenManager.ADO_BEARER_SOURCE = "AAD_BEARER_AZ_CLI"`.

**Stale-PAT fallback:** on HTTP 401/403 or a recognized Git authentication failure, APM tries an available az bearer. A successful bearer retry emits:

```
[!] ADO_APM_PAT was rejected for {host}; fell back to az cli bearer.
[!]     Consider unsetting the stale variable.
```

**Verbose source line** (one per host, emitted under `--verbose`):

```
[i] dev.azure.com -- using bearer from az cli (source: AAD_BEARER_AZ_CLI)
[i] dev.azure.com -- token from ADO_APM_PAT
```

**Diagnostic cases** (`build_error_context` in `src/apm_cli/core/auth.py`, `AdoAuthChainExhaustedError` in `src/apm_cli/core/auth_chain.py`):

Initial PAT/bearer diagnostics do not describe the later helper retry.

1. No PAT, no `az`: `Azure DevOps requires authentication. You have two options` -> install `az` and `az login`, or set `ADO_APM_PAT`.
2. No PAT, `az` not signed in: same two-options copy with `az login` first.
3. No PAT, wrong tenant: `Your az cli session (tenant: ...) returned a bearer token, but Azure DevOps rejected it (HTTP 401).` -> `az login --tenant <correct-tenant>`, or set `ADO_APM_PAT`.
4. PAT set, request failed: `ADO_APM_PAT is set, but the Azure DevOps request failed.` -> rotate the PAT, `az login` on Services, or store a GCM credential.
5. Helper empty or rejected: `AdoAuthChainExhaustedError` lists every chain step in order with its outcome (`Azure DevOps authentication failed. Tried in order: ...`) -> refresh PAT, `az login` on Services, or store a repository credential in GCM. DNS, TLS, timeout, and not-found failures re-raise without unlocking fill or becoming auth exhaustion.
6. Server, no PAT: `Azure DevOps Server requires ADO_APM_PAT or a Git credential helper.` -> set `ADO_APM_PAT` or store a GCM credential (`az` does not apply).
7. `az` installed but token acquisition failed: `Azure CLI is installed but could not get a token for Azure DevOps: <az reason>` (reason such as `AADSTS50076` from the provider's `last_error`; tenant lookup cached). Case 3 renders only when a bearer was actually issued.
