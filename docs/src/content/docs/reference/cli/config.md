---
title: apm config
description: Read and write APM CLI configuration
sidebar:
  order: 10
---

Read and write APM CLI configuration stored in `~/.apm/config.json`.

## Synopsis

```bash
apm config                       # show current configuration
apm config get [KEY]             # print one key, or the default listing
apm config list                  # print the default config listing
apm config set KEY VALUE         # write a key
apm config unset KEY             # remove a key
```

## Description

`apm config` manages the user-level CLI configuration file at `~/.apm/config.json`. It is independent of `apm.yml`, which describes a project. With no subcommand, `apm config` prints a table that combines:

- **Project** values from `apm.yml` in the current directory (when present): name, version, entrypoint, MCP dependency count, and compilation settings.
- **Global** values from `~/.apm/config.json`: CLI version, `temp-dir`, and any other set keys.

Use `get`/`set`/`unset` to manipulate individual keys. Boolean values accept `true`, `false`, `yes`, `no`, `1`, or `0`.

## Subcommands

### `apm config`

Show the merged project + global configuration as a table. Falls back to plain text if `rich` is unavailable.

### `apm config get [KEY]`

Print the value of `KEY`. With no argument, prints the same default user config listing as `apm config list`: effective values for the core user-settable keys that are useful to show. Noise-reduction defaults and unset optional values, such as the stable self-update channel, unset self-update install directory, and disabled transport preferences, are omitted until configured. Use `apm config get <KEY>` to inspect a specific key's effective value.

### `apm config list`

Print the default user config listing. This is an alias for `apm config get` with no `KEY`, provided for users who expect an `npm config list` or `git config --list` style command. The listing includes effective values such as `auto-integrate`, `temp-dir`, and `mcp-registry-url`, while suppressing noisy defaults and unset optional values until they are configured.

### `apm config set KEY VALUE`

Write `KEY` to `~/.apm/config.json`. Validates the value before writing:

- `temp-dir` must be an existing, writable directory. The path is expanded (`~`) and stored absolute.
- `target` must be a valid install target token (or comma-separated list), using the same validator as `apm install --target`.
- `self-update.channel` must be `stable` or `prerelease`.
- `self-update.install-dir` is expanded and stored as an absolute path. It becomes the default `APM_INSTALL_DIR` for `apm self-update` when the env var is not set.
- `copilot-cowork-skills-dir` must be absolute after expansion; the directory itself does not need to exist.
- `mcp-registry-url` must be an `http://` or `https://` URL with a valid host. All other schemes are rejected.
- Boolean keys reject anything outside the accepted truthy/falsy strings.

### `apm config unset KEY`

Remove `KEY` from `~/.apm/config.json`. No-op if the key is not set. Supported unset keys: `target`, `self-update.channel`, `self-update.install-dir`, `temp-dir`, `copilot-cowork-skills-dir`, `prefer-ssh`, `allow-protocol-fallback`, `audit-on-install`, `external.<name>.{llm,args}`, `mcp-registry-url`, and `registry.<name>.{url,token,default}`. After unsetting a key the effective value falls back to the environment variable, then the built-in default. Other boolean keys are reset by `set`-ing them to their default.

## Configuration keys

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `auto-integrate` | boolean | `true` | Auto-discover `.prompt.md` files under `.github/prompts/` and `.apm/prompts/` and merge them into compiled `AGENTS.md` output. |
| `target` | target token | unset | Default target for installs when `--target` and `apm.yml target(s)` are absent. Uses the same parser as `apm install --target` (single or comma-separated). |
| `self-update.channel` | enum | `stable` | Default release channel for `apm self-update`: `stable` uses GitHub's latest stable release; `prerelease` selects the newest non-draft prerelease and pins that version for the installer run. `APM_SELF_UPDATE_CHANNEL` overrides config. |
| `self-update.install-dir` | path | installer default | Default target directory passed to the self-update installer as `APM_INSTALL_DIR`. `APM_INSTALL_DIR` overrides config. |
| `temp-dir` | path | system temp | Directory used for clone and download operations. Useful when the OS temp directory is locked down (for example, corporate Windows endpoints rejecting `%TEMP%` with `[WinError 5]`). |
| `allow-protocol-fallback` | boolean | `false` | Enable the legacy cross-protocol fallback chain. When true, APM retries a failed clone with the opposite protocol (SSH -> HTTPS or HTTPS -> SSH). Equivalent to `--allow-protocol-fallback` or `APM_ALLOW_PROTOCOL_FALLBACK=1`. |
| `prefer-ssh` | boolean | `false` | Prefer SSH transport for shorthand (`owner/repo`) dependencies. Equivalent to `--ssh` or `APM_GIT_PROTOCOL=ssh`. |
| `copilot-cowork-skills-dir` | absolute path | auto-detected | Override the resolved Cowork OneDrive skills directory. Requires the `copilot-cowork` experimental flag for `set`. |
| `audit-on-install` | enum | `off` | Default content-audit mode for `apm install`: `off` / `warn` / `block`. `warn` records findings in the install summary; `block` halts on critical findings. Overridable per-install with `--audit` / `--no-audit`; an org policy `security.audit.on_install` floor can raise it. Requires the `external-scanners` experimental flag for `set`. |
| `external.<name>.llm` | boolean | unset | Opt a SARIF scanner into LLM-powered analysis (`<name>` validated against supported scanners). SkillSpector default is offline. LLM mode makes outbound API calls and needs `OPENAI_API_KEY` or `NVIDIA_INFERENCE_KEY`. Overridable per-run with `--external-llm` / `--no-external-llm`. Requires the `external-scanners` experimental flag. |
| `external.<name>.args` | string | unset | Extra scanner CLI flags, stored shlex-split as a list (e.g. `"--model gpt-4o"`). Allowlist-validated per adapter at run time. Overridable per-run with `--external-args`. Requires the `external-scanners` experimental flag. |
| `mcp-registry-url` | URL | public registry | Persist a private MCP registry endpoint. Accepts `http://` or `https://` URLs. Sits between `MCP_REGISTRY_URL` env and the built-in default in the resolution chain. Equivalent to exporting `MCP_REGISTRY_URL` permanently. |
| `registry.<name>.url` | URL | unset | Base URL for registry `<name>`. Requires `registries` experimental flag. |
| `registry.<name>.token` | string | unset | Bearer token for registry `<name>`. Stored in `~/.apm/config.json`; never in repo-tracked files. Requires `registries` experimental flag. |
| `registry.<name>.default` | boolean | `false` | Mark `<name>` as the user-scoped default registry. Only one registry may be default at a time; setting `true` clears any previous default. Requires `registries` experimental flag. |

### Resolution order

`temp-dir` and `copilot-cowork-skills-dir` are resolved at runtime as:

1. Environment variable (`APM_TEMP_DIR`, `APM_COPILOT_COWORK_SKILLS_DIR`)
2. Value in `~/.apm/config.json`
3. Built-in default (system temp / platform auto-detection)

`mcp-registry-url` follows a four-layer precedence chain (CLI flag wins):

1. `--registry <url>` flag on `apm mcp install` / `apm install --mcp` (this invocation only)
2. `MCP_REGISTRY_URL` environment variable
3. `mcp-registry-url` value in `~/.apm/config.json`
4. Built-in public default registry

`allow-protocol-fallback` and `prefer-ssh` follow the layered transport precedence:

1. CLI flag (`--allow-protocol-fallback`, `--ssh`) -- highest priority
2. Environment variable (`APM_ALLOW_PROTOCOL_FALLBACK=1`, `APM_GIT_PROTOCOL=ssh`)
3. Value in `~/.apm/config.json` (`apm config set ...`)
4. Built-in default (`false` / no preference)

Registry tokens are resolved as:

1. `APM_REGISTRY_TOKEN_<NAME>` environment variable (uppercase name, `-`/`.` -> `_`)
2. `registry.<name>.token` in `~/.apm/config.json`
3. Unauthenticated (APM surfaces a remediation hint on 401/403)

Registry URLs are merged at install time (highest wins):

1. `apm-policy.yml`
2. Project `apm.yml` `registries:` block
3. Workspace `~/.apm/apm.yml`
4. `registry.<name>.url` in `~/.apm/config.json`

Default registry selection (highest wins):

1. `registries.default` in project `apm.yml`
2. The registry entry in `~/.apm/config.json` with `"default": true` (set via `registry.<name>.default true`)

`apm self-update` installer preferences resolve as:

1. Environment variable (`APM_SELF_UPDATE_CHANNEL`, `APM_INSTALL_DIR`, or `VERSION`)
2. Non-secret config value (`self-update.channel`, `self-update.install-dir`)
3. Built-in default (`stable` channel / installer default directory)

Self-update config intentionally does **not** persist credentials, registry tokens, mirror URLs, commands, or installer arguments. Credentials continue through the existing auth path; mirror URLs remain invocation-scoped environment variables.

## Examples

Show everything:

```bash
apm config
```

Read and write `auto-integrate`:

```bash
apm config get auto-integrate
apm config set auto-integrate false
```

Persist a default install target:

```bash
apm config set target claude   # set the default once
apm config get target          # claude
apm install                    # no --target needed: deploys to claude
apm config unset target        # clear it (back to auto-detection)
```

Persist self-update installer preferences so prerelease upgrades and custom
installer locations do not require env vars on every run:

```bash
apm config set self-update.channel prerelease
apm config set self-update.install-dir ~/.local/bin
apm self-update
apm config unset self-update.channel
apm config unset self-update.install-dir
```

Persist SSH transport preference (no more `--ssh` on every install):

```bash
apm config set prefer-ssh true
apm config get prefer-ssh
# Remove the persisted preference:
apm config unset prefer-ssh
```

Persist cross-protocol fallback (useful when migrating from SSH to HTTPS or vice versa):

```bash
apm config set allow-protocol-fallback true
apm config get allow-protocol-fallback
```

Pin a writable temp directory on Windows:

```bash
apm config set temp-dir C:\apm-temp
apm config get temp-dir
```

Use the env var instead of persisting a value:

```bash
export APM_TEMP_DIR=/var/tmp/apm-work
apm install
```

Override the Cowork skills directory (experimental):

```bash
apm experimental enable copilot-cowork
apm config set copilot-cowork-skills-dir ~/Library/CloudStorage/OneDrive-Contoso/Cowork/skills
apm config unset copilot-cowork-skills-dir
```

Persist a private MCP registry URL (no more exporting the env var every session):

```bash
apm config set mcp-registry-url https://mcp.internal.example.com
apm config get mcp-registry-url
# Remove the persisted URL (falls back to MCP_REGISTRY_URL env, then the public default):
apm config unset mcp-registry-url
```

Configure a private registry (experimental):

```bash
apm experimental enable registries
apm config set registry.corp-main.url https://artifactory.corp.example.com/artifactory/api/apm/corp-main-local
apm config set registry.corp-main.token eyJ...
apm config set registry.corp-main.default true
apm config get registry.corp-main.url
apm config get registry.corp-main.default
apm config unset registry.corp-main.token
```

With URL, token, and default set in `config.json`, a project can omit the top-level `registries:` block from `apm.yml` and still route shorthand deps through `corp-main`. See [Registries](../../../guides/registries/).

Configure an external scanner (experimental):

```bash
apm experimental enable external-scanners
apm config set external.skillspector.llm true
apm config set external.skillspector.args "--model gpt-4o"
apm config get external.skillspector.llm
apm config unset external.skillspector.args
```

See [External scanners](../../../integrations/external-scanners/).

## Configuration file

- **Location:** `~/.apm/config.json`
- **Format:** JSON object, one entry per stored key.
- **Created on first read** with `{"default_client": "vscode"}`. Hand-editing is supported but `apm config set` is preferred -- it validates input and normalizes paths.

Internal JSON keys use snake_case (`auto_integrate`, `install_target`, `self_update_channel`, `self_update_install_dir`, `temp_dir`, `allow_protocol_fallback`, `prefer_ssh`, `copilot_cowork_skills_dir`); CLI keys use kebab-case or dotted namespaces (the CLI `target` key is stored as `install_target`). The CLI translates between the two.

## Related

- [`apm install`](../install/) -- consumes `target`, `temp-dir`, and `allow-protocol-fallback` / `prefer-ssh`.
- [`apm self-update`](../self-update/) -- consumes `self-update.channel` and `self-update.install-dir`.
- [`apm compile`](../compile/) -- affected by `auto-integrate`.
- [`apm experimental`](../experimental/) -- gates `copilot-cowork-skills-dir` and `registry.*` keys.
- [Environment variables](../../environment-variables/) -- `APM_ALLOW_PROTOCOL_FALLBACK`, `APM_GIT_PROTOCOL` are the env-var equivalents of the transport keys.
- [Registries](../../../guides/registries/) -- full private registry setup guide.
