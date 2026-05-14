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

Remove `KEY` from `~/.apm/config.json`. No-op if the key is not set. Only `temp-dir` and `copilot-cowork-skills-dir` are unsettable; boolean keys are reset by `set`-ing them to their default.

## Configuration keys

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `auto-integrate` | boolean | `true` | Auto-discover `.prompt.md` files under `.github/prompts/` and `.apm/prompts/` and merge them into compiled `AGENTS.md` output. |
| `temp-dir` | path | system temp | Directory used for clone and download operations. Useful when the OS temp directory is locked down (for example, corporate Windows endpoints rejecting `%TEMP%` with `[WinError 5]`). |
| `copilot-cowork-skills-dir` | absolute path | auto-detected | Override the resolved Cowork OneDrive skills directory. Requires the `copilot-cowork` experimental flag for `set`. |

### Resolution order

`temp-dir` and `copilot-cowork-skills-dir` are resolved at runtime as:

1. Environment variable (`APM_TEMP_DIR`, `APM_COPILOT_COWORK_SKILLS_DIR`)
2. Value in `~/.apm/config.json`
3. Built-in default (system temp / platform auto-detection)

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

## Configuration file

- **Location:** `~/.apm/config.json`
- **Format:** JSON object, one entry per stored key.
- **Created on first read** with `{"default_client": "vscode"}`. Hand-editing is supported but `apm config set` is preferred -- it validates input and normalizes paths.

Internal JSON keys use snake_case (`auto_integrate`, `temp_dir`, `copilot_cowork_skills_dir`); CLI keys use kebab-case. The CLI translates between the two.

## Related

- [`apm install`](../install/) -- consumes `temp-dir` for clone/download work.
- [`apm compile`](../compile/) -- affected by `auto-integrate`.
- [`apm experimental`](../experimental/) -- gates `copilot-cowork-skills-dir`.
