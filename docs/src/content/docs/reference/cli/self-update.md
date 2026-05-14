---
title: apm self-update
description: Self-update the APM CLI binary to the latest GitHub release.
sidebar:
  order: 5
---

Self-update the APM CLI binary to the latest GitHub release.

## Synopsis

```bash
apm self-update [--check]
```

## Description

`apm self-update` upgrades the **APM CLI itself** to the latest version published on GitHub releases. It downloads the official platform installer (`install.sh` on macOS/Linux, `install.ps1` on Windows) and runs it in place.

:::caution[Looking for dependency updates?]
This command does **not** update the packages declared in your `apm.yml`. To re-resolve your dependencies against the latest matching Git refs, run:

```bash
apm update
```

See [`apm update`](../update/) for the dependency refresh workflow, or [`apm install --frozen`](../install/) for a read-only, lockfile-pinned install.
:::

The command compares the installed version against the latest GitHub release and exits early if you are already current. With `--check`, it reports availability without installing.

:::note
Some package-manager distributions (for example, Homebrew) disable self-update at build time. In those builds, `apm self-update` prints a distributor-defined message (such as `brew upgrade apm`) and exits without running the installer. The startup update notification is also suppressed in those builds.
:::

## Options

| Flag | Description |
| --- | --- |
| `--check` | Only check whether a newer release exists. Print the result and exit without installing. |

## Examples

Check for an available update:

```bash
apm self-update --check
```

Install the latest release:

```bash
apm self-update
```

## Behavior

**Version check.** Fetches the latest release tag from GitHub and compares it to `apm --version`. If the installed version is current, the command exits with a success message and does nothing else.

**Download.** When an update is available (and `--check` is not set), the platform installer is downloaded into APM's temp directory, made executable, and invoked as a subprocess. The installer's stdout and stderr stream directly to your terminal so it can prompt for elevation when needed.

## Where the new binary lands

The installer writes the binary to the same location the original install script used -- typically `~/.apm/bin/apm` on macOS/Linux and `%LOCALAPPDATA%\Programs\apm\apm.exe` on Windows. Existing configuration under `~/.apm/` and your project files are untouched.

## After update

Restart your terminal (or re-resolve `apm` on `PATH`) and run `apm --version` to confirm the new version is active.

## Rollback

APM does not keep previous binaries. To roll back, reinstall a specific version using the manual installer:

```bash
# macOS / Linux
curl -sSL https://aka.ms/apm-unix | sh

# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -c "irm https://aka.ms/apm-windows | iex"
```

The installer scripts accept a version pin via environment variable -- see [Quickstart](../../../quickstart/).

## Failure modes

If GitHub is unreachable, the download fails, or the installer exits non-zero, `apm self-update` exits with code `1` and prints the manual update command. Your existing binary is unaffected.

## Startup update notification

APM checks for new releases at most once per day during normal command execution. When a newer version is available, you see:

```
A new version of APM is available: 0.7.0 (current: 0.6.3)
Run apm self-update to upgrade
```

The check is cached and non-blocking. It is suppressed in distributions that disable self-update.

## Related

- [`apm update`](../update/) -- refresh dependencies declared in `apm.yml` against the latest matching refs.
- [`apm install`](../install/) -- install dependencies; use `--frozen` for read-only, lockfile-pinned installs.
- [Quickstart](../../../quickstart/) -- first-time install.
