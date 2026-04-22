---
title: "apm experimental"
description: "Manage opt-in experimental feature flags. Evaluate new or changing behaviour without affecting APM defaults."
sidebar:
  order: 5
  label: "Experimental Flags"
---

`apm experimental` manages opt-in feature flags that gate new or changing behaviour. Flags let you evaluate a capability before it graduates to default, and can be toggled at any time without reinstalling APM.

Default APM behaviour never changes based on what is available here. A flag must be explicitly enabled to take effect, and every flag ships disabled.

:::caution[Scope]
Experimental flags are ergonomic and UX toggles only. They MUST NOT gate security-critical behaviour -- content scanning, path validation, lockfile integrity, token handling, MCP trust, or collision detection are never placed behind a flag. See [Security Model](../../enterprise/security/).
:::

## Subcommands

### `apm experimental list`

List every registered flag with its current state. This is the default when no subcommand is given.

```bash
apm experimental list [OPTIONS]
```

**Options:**
- `--enabled` - Show only flags that are currently enabled.
- `--disabled` - Show only flags that are currently disabled.
- `-v, --verbose` - Print the config file path used for overrides.

**Example:**

```bash
$ apm experimental list
                         Experimental Features
  Flag             Status     Description
  verbose-version  disabled   Show Python version, platform, and install path in 'apm --version'.
  Tip: apm experimental enable <name>
```

### `apm experimental enable`

Enable a flag. The override is persisted immediately.

```bash
apm experimental enable NAME
```

**Arguments:**
- `NAME` - Flag name. Accepted in either kebab-case (`verbose-version`) or snake_case (`verbose_version`).

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
apm experimental disable NAME
```

**Example:**

```bash
$ apm experimental disable verbose-version
[+] Disabled experimental feature: verbose-version
```

### `apm experimental reset`

Remove overrides and restore default state. With no argument, all overrides are cleared; a confirmation prompt lists exactly what will change.

```bash
apm experimental reset [NAME] [OPTIONS]
```

**Arguments:**
- `NAME` - Optional. Reset a single flag rather than all of them.

**Options:**
- `-y, --yes` - Skip the confirmation prompt (bulk reset only).

**Example:**

```bash
$ apm experimental reset
This will reset 1 experimental feature to their defaults:
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

| Name              | Description                                                                      |
|-------------------|----------------------------------------------------------------------------------|
| `verbose-version` | Show Python version, platform, and install path in `apm --version`.              |

New flags are proposed via [CONTRIBUTING.md](https://github.com/microsoft/apm/blob/main/CONTRIBUTING.md#how-to-add-an-experimental-feature-flag) and graduate to default when stable. See the contributor recipe for the full lifecycle.

## Storage and scope

Overrides are written to `~/.apm/config.json` under the `experimental` key and persist across CLI invocations. They are global to the user account and do not vary per project or per shell session. The canonical way to clear overrides is `apm experimental reset`; editing the file by hand is supported but unnecessary.

Pass `-v` / `--verbose` to any subcommand to print the config file path in use.

When a flag's behaviour is considered stable, it graduates: the gated code becomes the default path and the flag is removed from the registry in a future release.

## Troubleshooting

- **"Unknown experimental feature"** - the name is not in the registry. Run `apm experimental list` to see the current set. Suggestions printed below the error use fuzzy matching on registered names.
- **Unknown keys in config** - a flag that was enabled on a previous APM version may have been removed or renamed. `apm experimental list` surfaces a note when stale keys are present; `apm experimental reset` clears them.
