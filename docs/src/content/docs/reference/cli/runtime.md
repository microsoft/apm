---
title: apm runtime
description: Manage AI runtime CLIs (Copilot, Codex, LLM, Gemini) that execute APM scripts.
sidebar:
  order: 16
---

:::caution[Experimental]
`apm runtime` is experimental. Subcommand names, flags, and the supported runtime list may change.
:::

## Synopsis

```bash
apm runtime COMMAND [ARGS] [OPTIONS]
```

## Description

`apm runtime` manages the AI CLI binaries that execute the scripts declared in `apm.yml`. It installs them from official sources, records their location, and reports which one `apm run` will pick.

It does not install APM packages and does not deploy primitives. For that, see [`apm install`](../install/). For the `scripts:` block these runtimes execute, see the [quickstart](../../../quickstart/).

A runtime here is the AI CLI itself (`copilot`, `codex`, `llm`, `gemini`) -- the program that consumes the prompts and skills APM deploys. For the broader concept of harness targets that receive primitives, see [Primitives and targets](../../../concepts/primitives-and-targets/).

## Subcommands

| Command | Purpose |
|---|---|
| `setup RUNTIME` | Download and configure an AI runtime CLI. |
| `list` | List all known runtimes and their installation status. |
| `status` | Print the active runtime and the preference order `apm run` uses. |
| `remove RUNTIME` | Uninstall a runtime previously set up by APM. |

`RUNTIME` is one of: `copilot`, `codex`, `llm`, `gemini`.

### `apm runtime setup`

```bash
apm runtime setup RUNTIME [--version VERSION] [--vanilla]
```

Downloads the runtime binary from its official source and writes a default APM configuration that points at GitHub Models (free) where applicable. On Windows, setup scripts run through PowerShell automatically.

| Flag | Default | Description |
|---|---|---|
| `--version VERSION` | latest | Pin a specific upstream version. |
| `--vanilla` | off | Install the binary only. Skip APM-managed config; use the runtime's native defaults (e.g. OpenAI for Codex). |

### `apm runtime list`

```bash
apm runtime list
```

Prints a table of every supported runtime with its installation status, install path, and detected version.

### `apm runtime status`

```bash
apm runtime status
```

Prints the runtime preference order (`copilot -> codex -> gemini -> llm`) and the first runtime in that order that is currently installed. `apm run` uses this resolution to choose which CLI executes a script.

### `apm runtime remove`

```bash
apm runtime remove RUNTIME [-y]
```

Removes a runtime installed by `apm runtime setup`. Prompts for confirmation unless `-y` / `--yes` is passed. Does not touch runtimes installed outside APM.

## Examples

```bash
# Install Copilot CLI with APM defaults
apm runtime setup copilot

# Install Codex pinned to a specific version
apm runtime setup codex --version 0.20.0

# Install LLM with no APM-managed config
apm runtime setup llm --vanilla

# See what is installed and which one apm run will pick
apm runtime list
apm runtime status

# Uninstall without prompting
apm runtime remove gemini -y
```

## Supported runtimes

| Runtime | Description | Default config |
|---|---|---|
| `copilot` | GitHub Copilot CLI. | GitHub Models (free). |
| `codex` | OpenAI Codex CLI. | GitHub Models via global `~/.codex/config.toml`. |
| `llm` | Simon Willison's `llm` CLI with multiple providers. | GitHub Models. |
| `gemini` | Google Gemini CLI. | Native defaults. |

`--vanilla` skips the APM defaults column for any runtime and leaves the CLI configured as upstream ships it.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Setup, removal, listing, or status failed. The error message names the cause. |

## Related

- [`apm install`](../install/) -- install APM packages and deploy primitives to harness targets.
- [`apm run`](../run/) -- execute a script from `apm.yml` using the active runtime.
- [Quickstart](../../../quickstart/) -- end-to-end first run including `apm runtime setup`.
- [Primitives and targets](../../../concepts/primitives-and-targets/) -- how harnesses consume the primitives APM deploys.
