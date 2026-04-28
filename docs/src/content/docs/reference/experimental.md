---
title: "apm experimental"
description: "Manage opt-in experimental feature flags. Evaluate new or changing behaviour without affecting APM defaults."
sidebar:
  order: 6
  label: "Experimental Flags"
---

`apm experimental` manages opt-in feature flags that gate new or changing behaviour. Flags let you evaluate a capability before it graduates to default, and can be toggled at any time without reinstalling APM.

Default APM behaviour never changes based on what is available here. A flag must be explicitly enabled to take effect, and every flag ships disabled.

:::caution[Scope]
Experimental flags are ergonomic and UX toggles only. They MUST NOT gate security-critical behaviour -- content scanning, path validation, lockfile integrity, token handling, MCP trust, or collision detection are never placed behind a flag. See [Security Model](../../enterprise/security/).
:::

## Subcommands

### `apm experimental list`

List every registered flag with its current state. This is the default when no subcommand is given. Normal output is just the table; add `--verbose` to also print the config path and the introductory preamble.

```bash
apm experimental list [OPTIONS]
```

**Options:**
- `--enabled` - Show only flags that are currently enabled.
- `--disabled` - Show only flags that are currently disabled.
- `--json` - Emit a JSON array to stdout with `name`, `enabled`, `default`, `description`, and `source` fields.
- `-v, --verbose` - Print the config file path used for overrides and the introductory preamble.

**Example:**

```bash
$ apm experimental list
                         Experimental Features
  Flag             Status     Description
  verbose-version  disabled   Show Python version, platform, and install path in 'apm --version'.
  Tip: apm experimental enable <name>
```

Verbose output keeps the same table and adds the extra context lines:

```bash
$ apm experimental list --verbose
Config file: ~/.apm/config.json
Experimental features let you try new behaviour before it becomes default.
...table output...
```

Use `--json` for scripts and automation. It suppresses the table, colour, and intro preamble, and still honours `--enabled` / `--disabled` filters:

```bash
$ apm experimental list --json
[
  {
    "name": "verbose_version",
    "enabled": false,
    "default": false,
    "description": "Show Python version, platform, and install path in 'apm --version'.",
    "source": "default"
  }
]
```

The JSON `name` field uses the canonical registry key. For command arguments, APM still accepts either kebab-case (`verbose-version`) or snake_case (`verbose_version`). For clean machine-readable stdout, use `--json` without `--verbose`.

### `apm experimental enable`

Enable a flag. The override is persisted immediately.

```bash
apm experimental enable NAME [OPTIONS]
```

**Arguments:**
- `NAME` - Flag name. Accepted in either kebab-case (`verbose-version`) or snake_case (`verbose_version`).

**Options:**
- `-v, --verbose` - Print the config file path used for overrides.

**Example:**

```bash
$ apm experimental enable verbose-version
[+] Enabled experimental feature: verbose-version
Run 'apm --version' to see the new output.
```

Unknown names produce an error with suggestions drawn from the registered flag list:

```bash
$ apm experimental enable verbose-versio
[x] Unknown experimental feature: verbose-versio
Did you mean: verbose-version?
Run 'apm experimental list' to see all available features.
```

### `apm experimental disable`

Disable a flag. If the flag was not enabled, this is a no-op.

```bash
apm experimental disable NAME [OPTIONS]
```

**Options:**
- `-v, --verbose` - Print the config file path used for overrides.

**Example:**

```bash
$ apm experimental disable verbose-version
[+] Disabled experimental feature: verbose-version
```

### `apm experimental reset`

Remove overrides and restore default state. With no argument, all overrides are cleared; a confirmation prompt lists exactly what will change. Bulk reset also removes malformed overrides for registered flags, such as a string value where a boolean is expected.

```bash
apm experimental reset [NAME] [OPTIONS]
```

**Arguments:**
- `NAME` - Optional. Reset a single flag rather than all of them.

**Options:**
- `-y, --yes` - Skip the confirmation prompt (bulk reset only).
- `-v, --verbose` - Print the config file path used for overrides.

**Example:**

```bash
$ apm experimental reset
This will reset 1 experimental feature to its default:
  verbose-version (currently enabled -> disabled)
Proceed? [y/N]: y
[+] Reset all experimental features to defaults
```

Single-flag reset does not prompt:

```bash
$ apm experimental reset verbose-version
[+] Reset verbose-version to default (disabled)
```

## Example workflow

Try a flag, confirm its effect, then revert:

```bash
# 1. See what is available
apm experimental list

# 2. Opt in to verbose version output
apm experimental enable verbose-version

# 3. Observe the new behaviour
apm --version

# 4. Revert to default
apm experimental reset verbose-version
```

## Available flags

| Name                  | Description                                                                      |
|-----------------------|----------------------------------------------------------------------------------|
| `verbose-version`     | Show Python version, platform, and install path in `apm --version`.              |
| `copilot-cowork`      | Deploy APM skills to Microsoft 365 Copilot Cowork via OneDrive.                  |
| `marketplace-authoring`| Enable marketplace authoring commands (init, build, publish, etc.).              |

New flags are proposed via [CONTRIBUTING.md](https://github.com/microsoft/apm/blob/main/CONTRIBUTING.md#how-to-add-an-experimental-feature-flag) and graduate to default when stable. See the contributor recipe for the full lifecycle.
See also: [Cowork integration](../integrations/copilot-cowork/).

## Storage and scope

Overrides are written to `~/.apm/config.json` under the `experimental` key and persist across CLI invocations. They are global to the user account and do not vary per project or per shell session. The canonical way to clear overrides is `apm experimental reset`; editing the file by hand is supported but unnecessary.

Pass `-v` / `--verbose` to any subcommand after the subcommand name (for example `apm experimental list --verbose`) to print the config file path in use.

When a flag's behaviour is considered stable, it graduates: the gated code becomes the default path and the flag is removed from the registry in a future release.

## Troubleshooting

- **"Unknown experimental feature"** - the name is not in the registry. Run `apm experimental list` to see the current set. Suggestions printed below the error use fuzzy matching on registered names.
- **Unknown keys in config** - a flag that was enabled on a previous APM version may have been removed or renamed. `apm experimental list` surfaces a note when stale keys are present; `apm experimental reset` clears them.
- **Malformed values in config** - if a registered flag has a non-boolean override in `~/.apm/config.json`, `apm experimental reset --yes` removes the bad value and restores the default.
