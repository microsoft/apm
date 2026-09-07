---
title: apm self-update
description: Update a standalone APM CLI installation to the latest GitHub release.
sidebar:
  order: 5
---

Update a standalone APM CLI installation to the latest GitHub release.

## Synopsis

```bash
apm self-update [--check]
```

## Description

Use `apm self-update` for **standalone installs**. It downloads and runs the official
platform installer (`install.sh` on macOS/Linux, `install.ps1` on Windows).

:::note[Homebrew core]
The Homebrew core formula disables self-update, including `--check`, and startup
update notices. `apm self-update` prints a message directing you to
`brew upgrade apm` and exits without running the installer. For pip, Scoop, or
other package-manager installs, use the owning manager's upgrade command.
:::

:::caution[Looking for dependency updates?]
This command does **not** update the packages declared in your `apm.yml`. To re-resolve your dependencies against the latest matching versions or Git refs, run:

```bash
apm update
```

See [`apm update`](../update/) for the dependency refresh workflow, or [`apm install --frozen`](../install/) for a read-only, lockfile-pinned install.
:::

The command compares the installed version against the latest GitHub release and exits early if you are already current. With `--check`, it reports availability without installing.

Self-update can read two non-secret installer preferences from `apm config`:

- `self-update.channel`: `stable` (default) selects the newest stable release; `prerelease` selects the newest non-draft prerelease.
- `self-update.install-dir`: default target directory passed to the installer as `APM_INSTALL_DIR`.

`APM_SELF_UPDATE_CHANNEL` and `APM_INSTALL_DIR` override config. An explicit `VERSION` pins the release. Otherwise, either channel passes its selected release to the installer as one normalized `v<version>` value.

Credentials, registry tokens, mirror URLs, commands, and installer arguments are **not** persisted in self-update config. Tokens still resolve through the existing auth path; enterprise mirror URLs remain environment variables.

## Enterprise bootstrap mirrors

`apm self-update` uses the same mirror contract as the installer scripts. See the [installation bootstrap mirror section](../../../getting-started/installation/#enterprise-bootstrap-mirror-mode) for the canonical setup. When `APM_INSTALLER_BASE_URL` is set, it stays authoritative and APM appends only the platform script name. Otherwise, a resolved release uses its exact `v<version>` ref on GitHub or GHES; the `aka.ms` or current-ref fallback is used only when no release has been resolved.

| Variable | Default | Effect |
|----------|---------|--------|
| `APM_RELEASE_METADATA_URL` | _(unset)_ | Exact URL for mirrored release metadata, usually a static `latest.json` with at least `{"tag_name":"vX.Y.Z"}`. Overrides GitHub release metadata lookup. |
| `APM_INSTALLER_BASE_URL` | _(unset)_ | Authoritative base URL containing `install.sh` and `install.ps1`. APM appends only the platform script name, not a GitHub release ref. |
| `APM_RELEASE_BASE_URL` | _(unset)_ | Base URL containing release assets at `{base}/{tag}/{asset}`. Used when self-update runs the installer to fetch binary archives. |
| `APM_PYPI_INDEX_URL` | _(unset)_ | PyPI-compatible index used by installer pip fallback. |
| `APM_NO_DIRECT_FALLBACK` | _(unset)_ | Set to `1` to fail closed instead of using public GitHub, `aka.ms`, or PyPI fallback. |
| `APM_SELF_UPDATE_CHANNEL` | `stable` | Invocation-scoped channel override: `stable` or `prerelease`. Overrides `apm config set self-update.channel ...`. |
| `APM_INSTALL_DIR` | installer default | Invocation-scoped install target directory. Overrides `apm config set self-update.install-dir ...`. |
| `GITHUB_URL` | `https://github.com` | Legacy GitHub/GHES base URL. When the installer mirror is unset, a resolved release downloads the raw script from this host at its exact tag. |
| `APM_REPO` | `microsoft/apm` | Repository in `owner/repo` form for GitHub/GHES metadata and raw installer paths. |
| `VERSION` | _(unset)_ | Pin a release tag and skip release metadata lookup. |

Example:

```bash
export APM_RELEASE_METADATA_URL="https://artifactory.mycorp.example/generic/apm-releases/latest.json"
export APM_INSTALLER_BASE_URL="https://artifactory.mycorp.example/generic/apm-install"
export APM_RELEASE_BASE_URL="https://artifactory.mycorp.example/generic/apm-releases"
export APM_PYPI_INDEX_URL="https://artifactory.mycorp.example/api/pypi/python-proxy/simple"
export APM_NO_DIRECT_FALLBACK=1

apm self-update --check
apm self-update
```

With `APM_NO_DIRECT_FALLBACK=1`, missing or unreachable mirror settings are hard failures with a non-zero exit. For the full fail-closed scope, GHES token boundary, and no-egress smoke recipe, see [Enterprise bootstrap mirror mode](../../../getting-started/installation/#enterprise-bootstrap-mirror-mode).

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

Persist non-secret self-update defaults:

```bash
apm config set self-update.channel prerelease
apm config set self-update.install-dir ~/.local/bin
apm self-update
apm config unset self-update.channel
apm config unset self-update.install-dir
```

## Behavior

**Version check.** Fetches the latest release tag from GitHub and compares it to `apm --version`. If the installed version is current, the command exits with a success message and does nothing else.

**Download.** When an update is available (and `--check` is not set), the platform installer is downloaded into APM's temp directory, made executable, and invoked as a subprocess. The installer's stdout and stderr stream directly to your terminal so it can prompt for elevation when needed.

## Where the new binary lands

The installer writes to the same location the install script uses -- by default `/usr/local/bin/apm` on macOS/Linux. On Windows, self-update advances the stable executable path described in [Installation](../../../getting-started/installation/). Existing configuration under `~/.apm/` and your project files are untouched.

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

Release metadata failures exit `1`, including with `--check`: authentication (refresh credentials), rate limits (wait), HTTP/network errors (check endpoint/connectivity), or malformed JSON/metadata (verify source). Malformed `GITHUB_URL` produces a sanitized configuration diagnostic: use a valid HTTPS URL. HTTP 3xx reports that redirects are not followed, without echoing `Location`; see [mirror migration](../../../getting-started/installation/#enterprise-bootstrap-mirror-mode). See [Public release metadata](../../../getting-started/installation/#public-release-metadata) for retry restrictions. Download failures or non-zero installer exits also return `1` with mirror or manual update guidance. Your existing binary is unaffected.

## Startup update notification

APM checks for new releases at most once per day during normal command execution. When a newer version is available, you see:

```
A new version of APM is available: 0.7.0 (current: 0.6.3)
Run apm self-update to upgrade
```

The check is cached and non-blocking. Lookup, network, and metadata failures stay quiet. It is suppressed in distributions that disable self-update.

## Related

- [`apm update`](../update/) -- refresh dependencies declared in `apm.yml` against the latest matching versions or refs.
- [`apm install`](../install/) -- install dependencies; use `--frozen` for read-only, lockfile-pinned installs.
- [Quickstart](../../../quickstart/) -- first-time install.
