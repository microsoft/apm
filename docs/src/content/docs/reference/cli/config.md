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
apm config get [KEY]             # print one key, or all keys
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

Print the value of `KEY`. With no argument, prints all user-settable keys with their effective values (defaults included).

### `apm config set KEY VALUE`

Write `KEY` to `~/.apm/config.json`. Validates the value before writing:

- `temp-dir` must be an existing, writable directory. The path is expanded (`~`) and stored absolute.
- `copilot-cowork-skills-dir` must be absolute after expansion; the directory itself does not need to exist.
- Boolean keys reject anything outside the accepted truthy/falsy strings.

### `apm config unset KEY`

Remove `KEY` from `~/.apm/config.json`. No-op if the key is not set. Supported unset keys: `temp-dir`, `copilot-cowork-skills-dir`, `prefer-ssh`, `allow-protocol-fallback`, and `registry.<name>.{url,token,default}`. After unsetting a key the effective value falls back to the environment variable, then the built-in default. Other boolean keys are reset by `set`-ing them to their default.

## Configuration keys

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `auto-integrate` | boolean | `true` | Auto-discover `.prompt.md` files under `.github/prompts/` and `.apm/prompts/` and merge them into compiled `AGENTS.md` output. |
| `temp-dir` | path | system temp | Directory used for clone and download operations. Useful when the OS temp directory is locked down (for example, corporate Windows endpoints rejecting `%TEMP%` with `[WinError 5]`). |
| `allow-protocol-fallback` | boolean | `false` | Enable the legacy cross-protocol fallback chain. When true, APM retries a failed clone with the opposite protocol (SSH→HTTPS or HTTPS→SSH). Equivalent to `--allow-protocol-fallback` or `APM_ALLOW_PROTOCOL_FALLBACK=1`. |
| `prefer-ssh` | boolean | `false` | Prefer SSH transport for shorthand (`owner/repo`) dependencies. Equivalent to `--ssh` or `APM_GIT_PROTOCOL=ssh`. |
| `copilot-cowork-skills-dir` | absolute path | auto-detected | Override the resolved Cowork OneDrive skills directory. Requires the `copilot-cowork` experimental flag for `set`. |
| `registry.<name>.url` | URL | — | Base URL for registry `<name>`. Requires `registries` experimental flag. |
| `registry.<name>.token` | string | — | Bearer token for registry `<name>`. Stored in `~/.apm/config.json`; never in repo-tracked files. Requires `registries` experimental flag. |
| `registry.<name>.default` | boolean | `false` | Mark `<name>` as the user-scoped default registry. Only one registry may be default at a time; setting `true` clears any previous default. Requires `registries` experimental flag. |

### Resolution order

`temp-dir` and `copilot-cowork-skills-dir` are resolved at runtime as:

1. Environment variable (`APM_TEMP_DIR`, `APM_COPILOT_COWORK_SKILLS_DIR`)
2. Value in `~/.apm/config.json`
3. Built-in default (system temp / platform auto-detection)

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

## Configuration file

- **Location:** `~/.apm/config.json`
- **Format:** JSON object, one entry per stored key.
- **Created on first read** with `{"default_client": "vscode"}`. Hand-editing is supported but `apm config set` is preferred -- it validates input and normalizes paths.

Internal JSON keys use snake_case (`auto_integrate`, `temp_dir`, `allow_protocol_fallback`, `prefer_ssh`, `copilot_cowork_skills_dir`); CLI keys use kebab-case. The CLI translates between the two.

## Related

- [`apm install`](../install/) -- consumes `temp-dir` for clone/download work and `allow-protocol-fallback` / `prefer-ssh` for transport selection.
- [`apm compile`](../compile/) -- affected by `auto-integrate`.
- [`apm experimental`](../experimental/) -- gates `copilot-cowork-skills-dir` and `registry.*` keys.
- [Environment variables](../../environment-variables/) -- `APM_ALLOW_PROTOCOL_FALLBACK`, `APM_GIT_PROTOCOL` are the env-var equivalents of the transport keys.
- [Registries](../../../guides/registries/) -- full private registry setup guide.
