---
title: apm self-update
description: Self-update the APM CLI binary to the latest GitHub release.
sidebar:
  order: 5
---

## Synopsis

```bash
apm self-update [--check]
```

## Description

`apm self-update` upgrades the **APM CLI itself** to the latest version published on GitHub releases.

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
- `self-update.install-dir`: launcher directory passed as `APM_INSTALL_DIR`; on Unix it must match the existing installation, not redirect it.

`APM_SELF_UPDATE_CHANNEL` and `APM_INSTALL_DIR` override config. An explicit `VERSION` pins the release. Otherwise, either channel passes its selected release to the installer as one normalized `v<version>` value.

Credentials, registry tokens, mirror URLs, commands, and installer arguments are **not** persisted in self-update config. Tokens still resolve through the existing auth path; enterprise mirror URLs remain environment variables.

:::note
Some package-manager distributions (for example, Homebrew) disable self-update at build time. In those builds, `apm self-update` prints a distributor-defined message (such as `brew upgrade apm`) and exits without running the installer. The startup update notification is also suppressed in those builds.
:::

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
| `APM_INSTALL_DIR` | existing Unix launcher directory / Windows installer default | Overrides `self-update.install-dir`, but cannot redirect an existing Unix installation. |
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

Persist a release channel:

```bash
apm config set self-update.channel prerelease
apm self-update
apm config unset self-update.channel
```

## Behavior

When an update is available, APM downloads and runs the platform installer, streaming its output. On Unix, Python passes the running executable's identity and destination preferences without independently resolving destinations; `install.sh` remains the destination and ownership authority. Neither layer invokes `sudo`.

## Where the new binary lands

Recognized Unix bundles retain their launcher/bundle directories; fresh-install defaults do not relocate them. Conflicting overrides fail. See [ownership and migration](../../../getting-started/installation/#unix-install-ownership-and-migration) for permission checks, administrator updates, and pip fallback restrictions.

On Windows, self-update advances the [stable executable path](../../../getting-started/installation/). Configuration under `~/.apm/` and project files are untouched.

## After update

On Unix, follow the installer's final output. If the launcher is off `PATH`, run the exact `export PATH=...` command it prints or continue using the absolute launcher path.

On Windows, restart your terminal or run `apm --version` to verify the update.

## Rollback

APM does not keep previous binaries. Reinstall with a [version pin](../../../getting-started/installation/#installer-options), retaining the original Unix destinations. For package-managed installs, use the original package manager.

## Failure modes

Download or installer failures exit with code `1`. Unix ownership/destination refusals leave the existing installation untouched; follow the reported administrator/package-manager update action.

## Startup update notification

APM checks for new releases at most once per day during normal command execution. When a newer version is available, you see:

```
A new version of APM is available: 0.7.0 (current: 0.6.3)
Run apm self-update to upgrade
```

The check is cached and non-blocking. It is suppressed in distributions that disable self-update.

## Related

- [`apm update`](../update/) -- refresh dependencies declared in `apm.yml` against the latest matching versions or refs.
- [`apm install`](../install/) -- install dependencies; use `--frozen` for read-only, lockfile-pinned installs.
- [Quickstart](../../../quickstart/) -- first-time install.
