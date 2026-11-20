---
title: Install APM
description: Install the apm command on macOS, Linux or Windows, pin a version for CI, and upgrade or remove it later.
---

Install the `apm` command on your laptop, or pin a version on a CI runner. You also need
[git](https://git-scm.com/): APM downloads packages from git repositories. Prebuilt binaries
cover macOS, Linux and Windows on x86_64 and ARM64 (Windows ARM64 runs the x86_64 build).

## Install the CLI

### macOS and Linux

With [Homebrew](https://brew.sh/):

```bash frame="terminal"
brew install apm
```

Without Homebrew:

```bash frame="terminal"
curl -sSL https://aka.ms/apm-unix | sh
```

The installer writes `~/.local/bin/apm` and `~/.local/lib/apm` and never runs `sudo`. For
bash, zsh and fish it adds `~/.local/bin` to `PATH`; open a new shell afterwards. If it skips
that step, it prints the command to run.

### Windows

In PowerShell:

```powershell frame="terminal"
irm https://aka.ms/apm-windows | iex
```

The installer writes to `%LOCALAPPDATA%\Programs\apm` and adds it to your user `PATH`.

:::note[WinGet, Scoop and pip]
- WinGet: `winget install --id Microsoft.APM --exact --source winget`
- Scoop (main bucket): `scoop install apm`
- pip, with Python 3.11 or later: `python3 -m pip install apm-cli`

Each of these managers owns the install: upgrade and remove APM with the same manager.
:::

## Verify the install

```bash frame="terminal"
apm --version
```

On `apm: command not found`, open a new terminal or run the `PATH` command the installer
printed. If an older version answers, `type -a apm` lists every copy on your `PATH`.

Next: [Give every teammate the same skills, plugins and MCP servers](../../quickstart/).

## Check what the installer verifies

### Unix archive verification

`install.sh` downloads the release archive and its `.sha256` file from the same release, and
compares the archive's SHA-256 hash before it extracts anything. A missing, malformed or
mismatched checksum stops the install. This catches a corrupted or truncated download. It
does not protect you from a compromised release: the checksum comes from the same place as the
archive, and the installer checks no signature.

`install.ps1` checks the `.sha256` file when the release has one, and requires it for a pinned
version.

To read the script before it runs, save it to a file:

```bash frame="terminal"
curl -fsSL https://aka.ms/apm-unix -o install.sh
less install.sh
sh ./install.sh
```

### Installer options

| Option | Effect |
|---|---|
| `sh -s -- @v1.2.3` or `VERSION=v1.2.3` | Install that release, not the latest. Pin in CI. |
| `--prefix PATH` (Unix) | Launcher in `PATH/bin`, program in `PATH/lib/apm`. |
| `APM_INSTALL_DIR` | Launcher directory for a fresh install. |
| `APM_NO_MODIFY_PATH=1` (Unix) | Do not edit shell profiles. |
| `GITHUB_URL`, `APM_REPO` | Download from a GitHub Enterprise host or a fork. |

```bash frame="terminal"
curl -sSL https://aka.ms/apm-unix | sh -s -- @v1.2.3
```

```powershell frame="terminal"
$env:VERSION = "v1.2.3"; irm https://aka.ms/apm-windows | iex
```

Full list: [Environment variables](../../reference/environment-variables/).

### Unix install ownership and migration

The installer updates an existing APM in place, in its original directories. It refuses to
replace a copy it does not recognize or that a package manager such as Homebrew or pip owns:
update or remove that copy with its owner. For a system-wide install, choose the
destination explicitly:

```bash frame="terminal"
sudo sh ./install.sh --prefix /usr/local
```

In a container that runs as root, drop `sudo`. A root install without `--prefix` fails.

### Public release metadata

The installers and `apm self-update` find the latest release through the GitHub API. They send
`GITHUB_APM_PAT`, `GITHUB_TOKEN` or `GH_TOKEN` when set, and retry anonymously once after a
non-rate-limit 401 or 403. Mirrors never receive GitHub tokens. Package credentials are
separate: see [Use private sources](../authentication/).

## Upgrade APM

Upgrade with the tool that installed APM:

| Installed with | Upgrade |
|---|---|
| Homebrew | `brew upgrade apm` |
| `install.sh` or `install.ps1` | `apm self-update` |
| WinGet | `winget upgrade --id Microsoft.APM --exact --source winget` |
| Scoop | `scoop update apm` |
| pip | `python3 -m pip install --upgrade apm-cli` |

[`apm self-update`](../../reference/cli/self-update/) upgrades the CLI, not your packages
(that is `apm update`). On a Homebrew install it points you to `brew upgrade apm` instead.

### Migrate from the Microsoft tap

APM moved from the `microsoft/apm` tap to Homebrew core. To switch an unpinned tap install:

```bash frame="terminal"
brew list --formula --full-name      # shows microsoft/apm/apm
brew list --versions apm             # your installed version
brew info --formula homebrew/core/apm
brew reinstall homebrew/core/apm
brew list --formula --full-name      # now shows apm
type -a apm
```

:::caution[Stop if core is older]
`brew reinstall` can downgrade without warning: if core is older than your version, wait. If
linking fails on a conflicting file, core is installed but unlinked; resolve the conflict
without `--overwrite`, then run `brew link homebrew/core/apm`.
:::

Your `~/.apm/config.json` is unchanged. From then on, use `brew upgrade apm`.

## Uninstall APM

1. Optional: `apm cache clean` deletes downloaded packages.
2. Remove the CLI with its installer: `brew uninstall apm`,
   `winget uninstall --id Microsoft.APM --exact`, `scoop uninstall apm`, or
   `python3 -m pip uninstall apm-cli`.
3. For a standalone install, delete what it created:
   - macOS and Linux: `~/.local/bin/apm`, `~/.local/lib/apm` (confirm with `type -a apm`), and
     the block marked `apm shell setup` in your shell profile.
   - Windows: `%LOCALAPPDATA%\Programs\apm` and its `PATH` entries.

`~/.apm/` holds your user configuration and user-scope packages; delete it if you want them gone
too. Files APM deployed into your repositories stay.

## Install in containers and air-gapped networks

### Dev containers

Run the installer from `devcontainer.json`, and add `~/.local/bin` to `PATH` with `remoteEnv`:
without a terminal, the installer does not edit profiles.

```json title="devcontainer.json"
{
  "postCreateCommand": "curl -sSL https://aka.ms/apm-unix | sh"
}
```

For CI runners, see [Run in CI](../../consumer/run-in-ci/).

### Enterprise bootstrap mirror mode

Serve the installers and releases from an internal mirror, such as JFrog Artifactory, with no
public fallback:

```bash frame="terminal"
export APM_INSTALLER_BASE_URL="https://artifactory.example.com/generic/apm-install"
export APM_RELEASE_METADATA_URL="https://artifactory.example.com/generic/apm-releases/latest.json"
export APM_RELEASE_BASE_URL="https://artifactory.example.com/generic/apm-releases"
export APM_PYPI_INDEX_URL="https://artifactory.example.com/api/pypi/python-proxy/simple"
export APM_NO_DIRECT_FALLBACK=1

curl -sSL "$APM_INSTALLER_BASE_URL/install.sh" | sh
apm self-update --check
```

On Windows, set the same variables with `$env:` and run
`irm "$env:APM_INSTALLER_BASE_URL/install.ps1" | iex`. Copy each release's own `.sha256` files
next to its archives:

```text frame="none"
apm-releases/
  latest.json                      {"tag_name": "v0.36.1"}
  v0.36.1/
    apm-linux-x86_64.tar.gz
    apm-linux-x86_64.tar.gz.sha256
    apm-darwin-arm64.tar.gz
    apm-darwin-arm64.tar.gz.sha256
    apm-windows-x86_64.zip
    apm-windows-x86_64.zip.sha256
```

Metadata requests do not follow redirects: point `APM_RELEASE_METADATA_URL` at the final JSON,
or pin `VERSION`. `APM_NO_DIRECT_FALLBACK=1` blocks public GitHub, `aka.ms` and PyPI, but not a
GitHub Enterprise host in `GITHUB_URL`; set all four mirrors to keep every request internal.
These variables cover the CLI, not packages; for those, see
[Install through a proxy or offline](../../enterprise/registry-proxy/).

## If the install fails

- **glibc older than 2.28** (RHEL 7, Ubuntu 18.04): use a newer base image, or pip.
- **Windows `Access is denied` at `Testing binary...`**: AppLocker or App Control blocks
  user-folder programs. Ask your admin to allow `%LOCALAPPDATA%\Programs\apm\*`.
- Anything else: [Install failures](../../troubleshooting/install-failures/).
