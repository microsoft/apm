---
title: Environment variables
description: Documented public and operator-facing environment variables APM reads, with defaults, allowed values, and scope.
sidebar:
  order: 8
---

Single source of truth for documented public and operator-facing environment variables APM reads. Variables are grouped by purpose. Unless noted, scope is **process** (the running `apm` invocation and any child processes it spawns); a small number toggle behaviour for the entire shell **session**.

For CLI flags that pair with these variables, see the [reference index](./). For token policy and supply-chain guidance, see [Security model](../../enterprise/security/).

## Authentication

PAT / bearer credentials APM reads when cloning packages, calling host APIs, or wiring secrets into agent runtimes. Treat every value here as a secret: never commit, never log, never echo. See [Authentication](../../consumer/authentication/) for the resolution chain and [Security model](../../enterprise/security/) for token scoping.

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `GITHUB_APM_PAT` | Fine-grained PAT for APM module access on GitHub-class hosts (github.com, GHE Cloud, GHES). First in the GitHub `modules` precedence chain. | unset | Highest precedence for module clones. Also re-exported into Copilot / Codex runtimes. |
| `GITHUB_APM_PAT_<ORG>` | Per-org PAT override for github.com / GHE Cloud / GHES. `<ORG>` is the org name uppercased with non-alphanumeric chars replaced by `_`. | unset | Wins over `GITHUB_APM_PAT` when the package owner matches `<ORG>`. |
| `GITHUB_TOKEN` | Standard GitHub token. Falls back when `GITHUB_APM_PAT` is unset. Also forwarded to Codex. | unset | Used by `modules`, `copilot`, and `models` purposes. |
| `GH_TOKEN` | `gh` CLI token. Last in the GitHub `modules` chain before `gh auth token` and credential helpers. | unset | Forwarded to the Copilot runtime. |
| `GITHUB_COPILOT_PAT` | First-choice token for the Copilot runtime. | unset | Only consulted by the `copilot` purpose. |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | Forwarded to the Copilot runtime when present. | unset | Not part of the `modules` resolution chain. |
| `GITHUB_HOST` | GHES / GHE Cloud hostname (e.g. `ghe.example.com`). Switches host classification, transport selection, and auth chain. | `github.com` | Process-wide; affects clone URLs and host detection. |
| `GITLAB_APM_PAT` | APM module access on GitLab SaaS or self-managed. First in the `gitlab_modules` chain. | unset | |
| `GITLAB_TOKEN` | GitLab fallback token. | unset | |
| `GITLAB_HOST` | Single self-managed GitLab hostname (e.g. `gitlab.example.com`). | unset | Recognised as GitLab-class for transport / auth. |
| `APM_GITLAB_HOSTS` | Comma-separated list of additional GitLab hostnames to classify as GitLab-class. | unset | Use when you operate multiple GitLab instances. |
| `ADO_APM_PAT` | Azure DevOps PAT for `ado_modules`. | unset | Azure DevOps Services falls back to AAD bearer via `az`, then path-scoped `git credential fill`. Azure DevOps Server uses the PAT then fill; it does not use `az`. |
| `ADO_HOST` | Single on-prem Azure DevOps Server FQDN (e.g. `ado.company.com`). Registers the host as ADO-class for auth and API routing. | unset | Hostname only: no scheme, port, or path. Takes precedence over an overlapping `GITHUB_HOST`. |
| `APM_ADO_HOSTS` | Comma-separated list of on-prem ADO Server FQDNs, all classified as ADO-class. | unset | Entries are trimmed and matched case-insensitively; use for multiple Server instances. |
| `ARTIFACTORY_APM_TOKEN` | JFrog Artifactory token for `artifactory_modules`. | unset | Deprecated alias of `PROXY_REGISTRY_TOKEN`; sent only to the `PROXY_REGISTRY_URL` (or `ARTIFACTORY_BASE_URL`) origin; ignored with a warning when no proxy URL is set. |
| `GIT_SSH_COMMAND` | Standard git SSH command override. APM reads it before composing its own SSH env. | unset | If you set it, APM preserves your value. |
| `APM_GIT_CREDENTIAL_TIMEOUT` | Seconds to wait for a `git credential fill` response. | implementation default | Integer-like string; invalid values are ignored. |

## Transport and credential order

Controls how APM clones packages and enumerates refs on Git hosts: `APM_GIT_PROTOCOL` and `APM_ALLOW_PROTOCOL_FALLBACK` select the transport scheme, while `APM_GITHUB_AUTH_FIRST` changes credential order for exact-host `github.com` without affecting transport. These settings can also be persisted via [`apm config set`](../cli/config/) to avoid repeating flags or environment-variable exports.

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `APM_GIT_PROTOCOL` | Preferred Git protocol for cloning and ref enumeration on shorthand (`owner/repo`) dependencies. Accepted values: `ssh`, `https`. | unset | Equivalent to `--ssh` / `--https` flag. Resolution: CLI flag → env var → `prefer-ssh` key in `~/.apm/config.json` → git `insteadOf` rules → HTTPS. |
| `APM_ALLOW_PROTOCOL_FALLBACK` | Set to `1` (or `true`/`yes`/`on`) to enable the legacy cross-protocol fallback chain. When enabled, a failed clone is retried with the opposite protocol. | unset | Equivalent to `--allow-protocol-fallback`. Resolution: CLI flag → env var → `allow-protocol-fallback` key in `~/.apm/config.json` → `false`. |
| `APM_GITHUB_AUTH_FIRST` | Set to `1` (or `true`/`yes`/`on`) to skip the initial anonymous HTTPS attempt for exact-host `github.com` when a credential resolves. | unset | Equivalent to `--auth-first`. Resolution: CLI flag, then env var, then `github-auth-first` in `~/.apm/config.json`, then `false`. See [Authentication](../../getting-started/authentication/#tls-inspecting-proxy-blocks-the-anonymous-attempt). |

## TLS trust

APM verifies package-management HTTPS against the operating-system trust store by default, with bundled `certifi` as the Requests fallback. See [SSL / TLS issues](../../troubleshooting/ssl-issues/) for the full troubleshooting flow.

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `REQUESTS_CA_BUNDLE` | PEM bundle replacing APM's normal Requests trust. | unset | Wins over `CURL_CA_BUNDLE`, additive trust, and OS injection. |
| `CURL_CA_BUNDLE` | Replacement PEM bundle honored by Requests. | unset | Used when `REQUESTS_CA_BUNDLE` is unset; suppresses additive trust and OS injection. |
| `APM_DISABLE_TRUSTSTORE` | Set to `1` (or `true`/`yes`/`on`) to disable OS/additive trust. | unset | Restores bundled `certifi` unless an explicit Requests/curl replacement is set. |
| `APM_EXTRA_CA_BUNDLE` | Certificate-only PEM bundle added to APM package-management HTTPS. | unset | Retains OS roots, or `certifi` roots on Requests fallback. Invalid selected input fails closed: it must be a readable, non-empty regular file, no larger than 8 MiB, containing ASCII PEM certificates and no private keys. |

Trust resolution is ordered: `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `APM_DISABLE_TRUSTSTORE`, `APM_EXTRA_CA_BUNDLE`, then the normal OS/`certifi` defaults. Higher-precedence controls suppress additive-bundle validation. Unset or blank `APM_EXTRA_CA_BUNDLE` preserves existing behavior.

The additive setting does not derive trust settings for `apm run` children or configure Git, Node, or Rust. Those retain their existing settings; see [runtime coverage](../../troubleshooting/ssl-issues/#runtime-coverage). For isolated `github.com` Git attempts, APM inherits your Git `http.sslBackend`, `http.sslCAInfo`, and `http.sslCAPath`; see [Authentication](../../getting-started/authentication/#tls-inspecting-proxy-blocks-the-anonymous-attempt).

## Registry (MCP and proxy)

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `MCP_REGISTRY_URL` | Override the MCP registry endpoint used by `apm mcp`, `apm install --mcp NAME`, and manifest-driven `dependencies.mcp` installs. HTTPS by default; HTTP requires `MCP_REGISTRY_ALLOW_HTTP=1`. | public registry | When set, registry commands print `Registry: <url> (from MCP_REGISTRY_URL)` and installs print `Using MCP registry: <url> (from MCP_REGISTRY_URL)`. See [`apm mcp`](../cli/mcp/). |
| `MCP_REGISTRY_ALLOW_HTTP` | Set to `1` to permit a plaintext `http://` `MCP_REGISTRY_URL` (development only). | unset | Required to opt in to HTTP; production should always use HTTPS. |
| `MCP_REGISTRY_CONNECT_TIMEOUT` | Connect timeout for registry HTTP calls, in seconds (float). | implementation default | Non-positive / non-numeric values are ignored. |
| `MCP_REGISTRY_READ_TIMEOUT` | Read timeout for registry HTTP calls, in seconds (float). | implementation default | Non-positive / non-numeric values are ignored. |
| `PROXY_REGISTRY_URL` | Enterprise package proxy base URL. See [Registry proxy](../../enterprise/registry-proxy/). | unset | When set, APM resolves package downloads through the proxy. |
| `PROXY_REGISTRY_TOKEN` | Bearer token for `PROXY_REGISTRY_URL`. | unset | Required for authenticated proxies. |
| `PROXY_REGISTRY_ALLOW_HTTP` | Allow `http://` for `PROXY_REGISTRY_URL` (development only). | unset | Mirrors the MCP registry's HTTP opt-in. |
| `PROXY_REGISTRY_ONLY` | Set to `1` to refuse any download not served by the proxy. | unset | Air-gapped deployments. |
| `APM_REGISTRY_TOKEN_<NAME>` | Bearer token for configured package registry `<NAME>`. | unset | `<NAME>` is normalized to uppercase with non-alphanumerics replaced by `_`. |
| `APM_REGISTRY_USER_<NAME>` / `APM_REGISTRY_PASS_<NAME>` | Basic-auth credentials for configured package registry `<NAME>`. | unset | Used when a bearer token is not supplied. |
| `ARTIFACTORY_BASE_URL` | Legacy alias for `PROXY_REGISTRY_URL`. | unset | Prefer `PROXY_REGISTRY_URL` in new setups. |
| `ARTIFACTORY_ONLY` | Legacy alias for `PROXY_REGISTRY_ONLY`. | unset | Prefer `PROXY_REGISTRY_ONLY`. |
| `ARTIFACTORY_MAX_ARCHIVE_MB` | Maximum archive size accepted from Artifactory, in MB. | `500` | Integer-like string. |

## Cache and filesystem

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `APM_CACHE_DIR` | Override the APM cache root. | platform default (XDG / `LOCALAPPDATA`) | Must be writable. See [`apm cache`](../cli/cache/). |
| `APM_NO_CACHE` | `1`/`true`/`yes` disables read and write of the cache for the current invocation. | unset | Equivalent to `--no-cache` on commands that support it. |
| `APM_TEMP_DIR` | Override the temp directory used by clone and download operations. | system default | Useful on Windows when endpoint security blocks `%TEMP%`. Resolution: env var > `temp_dir` in `~/.apm/config.json` > system default. |
| `APM_HOME` | Override the root for APM-owned user metadata and configuration. | `~/.apm` | Must be writable. Does not redirect files deployed to agent targets. |
| `APM_NO_REFLINK` | Any non-empty value disables copy-on-write (reflink) optimisation; APM falls back to plain copies. | unset | Diagnostic / portability escape hatch. |
| `APM_COPILOT_COWORK_SKILLS_DIR` | Override the destination directory for Copilot cowork skills. | platform auto-detect | Resolution: env var > config > auto-detect. |
| `APM_COPILOT_APP_DB` | Override the path to the GitHub Copilot desktop App SQLite database used by the `copilot-app` target. | platform auto-detect | Useful for tests or non-standard Copilot installs. Resolution: env var > auto-detect. |
| `COPILOT_HOME` | Override the GitHub Copilot CLI home used by Copilot target detection and user-scope writes. | platform auto-detect | Read by the Copilot integration target. |
| `CODEX_HOME` | Override the Codex home used for user-scope writes: `apm compile --global` (`AGENTS.md`) and `apm install -g` agents, hooks, and MCP config (`config.toml`). | `~/.codex` | Nonblank values must be absolute. Blank or unset uses `~/.codex`. Codex skills stay in `~/.agents/skills/`. Auto-detection still checks `~/.codex/`; otherwise use `--target codex` or `targets:`. |
| `HERMES_HOME` | Override the Hermes home used by user-scope skills and MCP config. | `~/.hermes` | Used by the explicit `hermes` target; it does not enable auto-detection. |
| `APM_BROAD_FETCH_DEPTH` | Maximum commit depth used by the bare-cache broad fetch when resolving git refs. | `50` | Integer-like string; tune for very deep histories where ref resolution misses. |
| `XDG_CACHE_HOME` | Standard XDG base-directory variable APM consults when `APM_CACHE_DIR` is unset (Linux / macOS). | unset | Honoured per the XDG spec. |
| `LOCALAPPDATA` | Standard Windows variable APM consults when `APM_CACHE_DIR` is unset. | OS-provided | Used to derive the default Windows cache path. |
| `CLAUDE_CONFIG_DIR` | Override the user-scope destination Claude reads for skills, agents, and MCP config. | Claude default | For MCP config, nonblank values must be absolute. User-scope MCP servers are managed in `$CLAUDE_CONFIG_DIR/.claude.json` (including installation and stale cleanup); when unset or blank, APM uses `~/.claude.json`. |

### `APM_HOME` path contract

`APM_HOME` is the single root for APM-owned user metadata. When it is unset or
empty, the root is `~/.apm`. When set, global commands and lifecycle processing
use the same overridden root:

| State | Path |
|---|---|
| User manifest | `$APM_HOME/apm.yml` |
| User lockfile | `$APM_HOME/apm.lock.yaml` |
| Installed package metadata and content | `$APM_HOME/apm_modules/` |
| User configuration | `$APM_HOME/config.json` |
| Marketplace registrations | `$APM_HOME/marketplaces.json` |
| Marketplace cache and version pins | `$APM_HOME/cache/marketplace/` |
| Lifecycle trust | `$APM_HOME/scripts-trust.json` |
| Lifecycle logs | `$APM_HOME/logs/` |
| Lifecycle lock and deployment receipts | `$APM_HOME/.apm-lifecycle.lock`, `$APM_HOME/deployment-receipts/` |

This applies to user-scope operations such as `apm install --global`, `apm
update --global`, `apm uninstall --global`, `apm lock --global`, `apm deps ...
--global`, `apm compile --global`, `apm marketplace`, `apm lifecycle`, and `apm
config`. `apm prune` is project-scope only. Other pages write `~/.apm` for the
default location.

A leading `~` is expanded, and a relative `APM_HOME` resolves against the
directory APM started in, so commands that change directory keep using the same
root. If that directory no longer exists, APM exits with an error; set an
absolute `APM_HOME` instead. Changing `APM_HOME` does not
move an existing `~/.apm`; copy the files yourself if you want to keep them.

These paths are outside the contract:

- The download cache follows `APM_CACHE_DIR` (see above); the update-check
  timestamp stays in the operating-system cache directory.
- Runtimes installed by `apm runtime setup` stay under `~/.apm/runtimes/`.
- `apm install --global` keeps its installation identity at `~/.apm-local/`,
  beside the deployment root, like deployed target files.
- State-changing commands also hold `~/.apm-local/.apm-lifecycle.lock`, so
  commands run with different `APM_HOME` values wait for each other instead of
  racing on the same project or target directories.

`APM_HOME` does **not** redirect target deployment. Precedence:

1. APM metadata: `APM_HOME`, then `~/.apm`.
2. Target files: the target's own home variable when it has one (for example
   `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, or `HERMES_HOME`), then the
   operating-system home.

The two families never fall back to each other: a target variable does not
move APM metadata, and `APM_HOME` does not move target files.

To isolate APM metadata for a temporary global install:

```bash
export APM_HOME="$(mktemp -d)/apm"
apm install --global example-org/example-package
```

The manifest, lockfile, modules, configuration, and lifecycle state remain
under `$APM_HOME`; deployed target files still go to their normal user-scope
destinations. For complete filesystem isolation, also run with an isolated
operating-system home and/or set the selected targets' home variables.

## Policy

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `APM_POLICY_DISABLE` | Set to `1` to skip policy discovery and enforcement for **the entire shell session**. Loudly logged. | unset | Equivalent to the per-invocation `--no-policy` on commands that expose it. The only escape hatch for `apm deps update`. See [`apm policy`](../cli/policy/). |
| `APM_GITLAB_POLICY_REPO` | Override the org policy project name auto-discovered on GitLab (gitlab.com and self-managed). | `apm-policy` | A single project-name segment only; no slash or leading `.` / `_`. GitLab rejects the GitHub-family candidate cascade (`.github-private`, `.github`, `.apm`, `_apm`). See [Policy Files](../../enterprise/apm-policy/#where-it-lives). |

## External scanners

These keys are consumed by a third-party SARIF scanner (e.g. SkillSpector), not
by APM itself. APM forwards them to the scanner subprocess **only** when LLM
mode is active for that run (`apm audit --external <name> --external-llm` or
`external.<name>.llm true`); otherwise they are stripped from the scanner's
environment. APM never stores them. Requires the `external-scanners`
experimental flag.

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `OPENAI_API_KEY` | API key SkillSpector uses for LLM-powered analysis. | unset | Forwarded only when LLM mode is active. If `--external-llm` is set and no key is present, the scan fails closed. |
| `NVIDIA_INFERENCE_KEY` | Alternative API key SkillSpector accepts for LLM-powered analysis. | unset | Same forwarding / fail-closed semantics as `OPENAI_API_KEY`. |

## Debugging and output

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `APM_DEBUG` | Any non-empty value enables low-level debug logging in download, file ops, and clone-cache paths. | unset | Verbose; use for troubleshooting only. |
| `APM_LOG_LEVEL` | Override the CLI logging level. | implementation default | Debugging escape hatch. |
| `APM_VERBOSE` | `1` enables verbose output for the install pipeline. APM also sets this internally when `--verbose` is passed. | unset | |
| `APM_PROGRESS` | Force the install TUI on or off: `always`/`on`/`1`/`true`/`yes` to force on; `never`/`quiet`/`off`/`0`/`false`/`no` to force off; `auto` (default) lets APM decide based on `CI`, `TERM`, and TTY. | `auto` | The CLI sets `APM_PROGRESS=quiet` when `--quiet` is passed. |
| `CI` | Standard CI marker. When truthy (`1`/`true`/`yes`), APM disables the install TUI and adjusts a few interactive defaults. | unset | Read; never written. |
| `TERM` | Standard terminal type. `""` or `dumb` disables the install TUI. | OS-provided | Read; never written. |

## Experimental and internal

These variables exist in the codebase but are not part of the documented contract. Behaviour and naming may change without notice. Do not rely on them in production scripts.

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `APM_RESOLVE_PARALLEL` | Tunes parallelism in the dependency resolver. | implementation default | Subject to change. |
| `APM_TIERED_RESOLVER` | Set to `0`/`false`/`no`/`off` to disable the tiered git-ref resolver (per-run cache + commits API + bare `rev-parse` + legacy clone) and force every `install`/`update`/`outdated` ref resolution through the legacy shallow-clone path. Emergency rollback for #1369. | `1` (on) | Subject to change. Removal expected once the tiered stack has soaked through a release. |
| `APM_LEGACY_SKILL_PATHS` | Toggles legacy skill-path layout in integration targets. | unset | Compatibility shim; will be removed. |
| `APM_NO_SCRIPTS` | Disables all lifecycle scripts (install, update, uninstall, and compile). | unset | Internal safety/test switch; prefer executable trust policy in production. |
| `APM_NON_INTERACTIVE` | Forces non-interactive behavior. | unset | Used by automation and tests. |
| `APM_E2E_TESTS` | Marks the process as an end-to-end test run; relaxes some interactive guards. | unset | Test harness only. Do not set in normal use. |
