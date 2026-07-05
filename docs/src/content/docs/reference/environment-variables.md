---
title: Environment variables
description: Documented public and operator-facing environment variables APM reads, with defaults, allowed values, and scope.
sidebar:
  order: 8
---

Single source of truth for documented public and operator-facing environment variables APM reads. Variables are grouped by purpose. Unless noted, scope is **process** (the running `apm` invocation and any child processes it spawns); a small number toggle behaviour for the entire shell **session**.

For CLI flags that pair with these variables, see the [reference index](./). For token policy and supply-chain guidance, see [Security model](../enterprise/security/).

## Authentication

PAT / bearer credentials APM reads when cloning packages, calling host APIs, or wiring secrets into agent runtimes. Treat every value here as a secret: never commit, never log, never echo. See [Authentication](../consumer/authentication/) for the resolution chain and [Security model](../enterprise/security/) for token scoping.

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
| `ADO_APM_PAT` | Azure DevOps PAT for `ado_modules`. | unset | If unset, APM falls back to AAD bearer via `az` CLI. |
| `ARTIFACTORY_APM_TOKEN` | JFrog Artifactory token for `artifactory_modules`. | unset | Also used by the registry-proxy resolver. |
| `GIT_SSH_COMMAND` | Standard git SSH command override. APM reads it before composing its own SSH env. | unset | If you set it, APM preserves your value. |
| `APM_GIT_CREDENTIAL_TIMEOUT` | Seconds to wait for a `git credential fill` response. | implementation default | Integer-like string; invalid values are ignored. |

## Transport and protocol

Controls how APM clones packages from Git hosts. These settings can also be persisted via [`apm config set`](./cli/config/) to avoid repeating flags or environment-variable exports.

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `APM_GIT_PROTOCOL` | Preferred clone protocol for shorthand (`owner/repo`) dependencies. Accepted values: `ssh`, `https`. | unset | Equivalent to `--ssh` / `--https` flag. Resolution: CLI flag → env var → `prefer-ssh` key in `~/.apm/config.json` → git `insteadOf` rules → HTTPS. |
| `APM_ALLOW_PROTOCOL_FALLBACK` | Set to `1` (or `true`/`yes`/`on`) to enable the legacy cross-protocol fallback chain. When enabled, a failed clone is retried with the opposite protocol. | unset | Equivalent to `--allow-protocol-fallback`. Resolution: CLI flag → env var → `allow-protocol-fallback` key in `~/.apm/config.json` → `false`. |

## TLS trust

APM verifies HTTPS against the operating-system trust store by default. For the full troubleshooting flow, see [SSL / TLS issues](../troubleshooting/ssl-issues/).

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `REQUESTS_CA_BUNDLE` | PEM bundle for APM's Python HTTP requests. | unset | Explicit override; wins over OS trust-store injection. Use for a per-shell corporate CA bundle. |
| `CURL_CA_BUNDLE` | PEM bundle fallback honoured by `requests`. | unset | Explicit override; wins over OS trust-store injection when `REQUESTS_CA_BUNDLE` is unset. |
| `APM_DISABLE_TRUSTSTORE` | Set to `1` (or `true`/`yes`/`on`) to disable OS trust-store injection. | unset | Escape hatch that restores the legacy bundled-`certifi` verification path. |

## Registry (MCP and proxy)

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `MCP_REGISTRY_URL` | Override the MCP registry endpoint used by `apm mcp` and `apm install --mcp NAME`. Must be `https://`. | public registry | When set, every `apm mcp` command prints `Registry: <url>`. See [`apm mcp`](./cli/mcp/). |
| `MCP_REGISTRY_ALLOW_HTTP` | Set to `1` to permit a plaintext `http://` `MCP_REGISTRY_URL` (development only). | unset | Required to opt in to HTTP; production should always use HTTPS. |
| `MCP_REGISTRY_CONNECT_TIMEOUT` | Connect timeout for registry HTTP calls, in seconds (float). | implementation default | Non-positive / non-numeric values are ignored. |
| `MCP_REGISTRY_READ_TIMEOUT` | Read timeout for registry HTTP calls, in seconds (float). | implementation default | Non-positive / non-numeric values are ignored. |
| `PROXY_REGISTRY_URL` | Enterprise package proxy base URL. See [Registry proxy](../enterprise/registry-proxy/). | unset | When set, APM resolves package downloads through the proxy. |
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
| `APM_CACHE_DIR` | Override the APM cache root. | platform default (XDG / `LOCALAPPDATA`) | Must be writable. See [`apm cache`](./cli/cache/). |
| `APM_NO_CACHE` | `1`/`true`/`yes` disables read and write of the cache for the current invocation. | unset | Equivalent to `--no-cache` on commands that support it. |
| `APM_TEMP_DIR` | Override the temp directory used by clone and download operations. | system default | Useful on Windows when endpoint security blocks `%TEMP%`. Resolution: env var > `temp_dir` in `~/.apm/config.json` > system default. |
| `APM_HOME` | Override the APM home directory used for user config and state. | platform default | Must be writable. |
| `APM_NO_REFLINK` | Any non-empty value disables copy-on-write (reflink) optimisation; APM falls back to plain copies. | unset | Diagnostic / portability escape hatch. |
| `APM_COPILOT_COWORK_SKILLS_DIR` | Override the destination directory for Copilot cowork skills. | platform auto-detect | Resolution: env var > config > auto-detect. |
| `APM_COPILOT_APP_DB` | Override the path to the GitHub Copilot desktop App SQLite database used by the `copilot-app` target. | platform auto-detect | Useful for tests or non-standard Copilot installs. Resolution: env var > auto-detect. |
| `COPILOT_HOME` | Override the GitHub Copilot CLI home used by Copilot target detection and user-scope writes. | platform auto-detect | Read by the Copilot integration target. |
| `CODEX_HOME` | Override the Codex home used by Codex target detection and user-scope writes. | platform auto-detect | Read by the Codex integration target. |
| `HERMES_HOME` | Override the Hermes home used by Hermes target detection and user-scope writes. | platform auto-detect | Requires the `hermes` experimental flag for Hermes deployment. |
| `APM_BROAD_FETCH_DEPTH` | Maximum commit depth used by the bare-cache broad fetch when resolving git refs. | `50` | Integer-like string; tune for very deep histories where ref resolution misses. |
| `XDG_CACHE_HOME` | Standard XDG base-directory variable APM consults when `APM_CACHE_DIR` is unset (Linux / macOS). | unset | Honoured per the XDG spec. |
| `LOCALAPPDATA` | Standard Windows variable APM consults when `APM_CACHE_DIR` is unset. | OS-provided | Used to derive the default Windows cache path. |
| `CLAUDE_CONFIG_DIR` | Override the destination Claude reads for skills / agents. | Claude default | Read by the Claude integration target. |

## Policy

| Variable | Purpose | Default | Notes |
|---|---|---|---|
| `APM_POLICY_DISABLE` | Set to `1` to skip policy discovery and enforcement for **the entire shell session**. Loudly logged. | unset | Equivalent to the per-invocation `--no-policy` on commands that expose it. The only escape hatch for `apm deps update`. See [`apm policy`](./cli/policy/). |

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
| `APM_NO_SCRIPTS` | Disables package lifecycle script execution. | unset | Internal safety/test switch; prefer executable trust policy in production. |
| `APM_NON_INTERACTIVE` | Forces non-interactive behavior. | unset | Used by automation and tests. |
| `APM_E2E_TESTS` | Marks the process as an end-to-end test run; relaxes some interactive guards. | unset | Test harness only. Do not set in normal use. |
