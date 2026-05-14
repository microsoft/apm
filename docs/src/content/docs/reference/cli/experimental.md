---
title: apm experimental
description: Manage opt-in experimental feature flags stored in ~/.apm/config.json
sidebar:
  order: 24
---

Manage opt-in experimental feature flags. Flags gate new or changing behaviour so you can evaluate it before it graduates to default.

:::caution
Experimental flags can change behaviour, default value, or be removed entirely in any release. Do not depend on them in scripts or CI without pinning the APM CLI version.
:::

## Synopsis

```bash
apm experimental                       # alias for `list`
apm experimental list [OPTIONS]
apm experimental enable NAME
apm experimental disable NAME
apm experimental reset [NAME] [--yes]
```

## Description

Experimental flags live under the `experimental` key of `~/.apm/config.json` and default to disabled. Toggling a flag persists the override; `reset` removes it. Flag names are case-insensitive and accept either kebab-case (`verbose-version`) or snake_case (`verbose_version`) on the command line.

Flags never gate security-critical behaviour (content scanning, lockfile integrity, token handling, MCP trust checks). Those are always on. See [`apm audit`](../audit/) for the security model.

## Subcommands

### `apm experimental list`

List all registered flags with their current status.

| Option | Description |
| --- | --- |
| `--enabled` | Show only enabled flags. Mutually exclusive with `--disabled`. |
| `--disabled` | Show only disabled flags. |
| `--json` | Emit a JSON array (`name`, `enabled`, `default`, `description`, `source`). |
| `-v`, `--verbose` | Show config file path and extra context. |

### `apm experimental enable NAME`

Enable a flag and persist the override. Prints a hint pointing to the relevant command when one is registered. Errors with `did you mean` suggestions if `NAME` is not registered.

### `apm experimental disable NAME`

Disable a flag and persist the override.

### `apm experimental reset [NAME]`

Reset one flag (when `NAME` is given) or all flags (when omitted) to registry defaults. Bulk reset prompts for confirmation and also removes unknown or malformed entries from `~/.apm/config.json`.

| Option | Description |
| --- | --- |
| `-y`, `--yes` | Skip the confirmation prompt on bulk reset. |
| `-v`, `--verbose` | Show config file path and extra context. |

## Group options

| Option | Description |
| --- | --- |
| `-v`, `--verbose` | Inherited by every subcommand unless overridden locally. |

## Available flags

| Flag | What it does | Related command |
| --- | --- | --- |
| `verbose-version` | Adds Python version, platform, and install path to `apm --version` output. | `apm --version` |
| `copilot-cowork` | Enables Microsoft 365 Copilot Cowork skill deployment via OneDrive. | `apm compile --target copilot-cowork --global` |

Run `apm experimental list` to see the live registry; new flags ship in minor releases.

## Examples

List all flags:

```bash
apm experimental list
```

Enable verbose version output:

```bash
apm experimental enable verbose-version
apm --version
```

Show only enabled flags as JSON:

```bash
apm experimental list --enabled --json
```

Reset a single flag:

```bash
apm experimental reset verbose-version
```

Reset everything without prompting:

```bash
apm experimental reset --yes
```

## Related

- [`apm config`](../config/) -- read and write user-level CLI configuration.
- [Experimental flags reference](../../experimental/) -- detailed notes on each flag and its graduation status.
- [`apm compile`](../compile/) -- target consumers of the `copilot-cowork` flag.
