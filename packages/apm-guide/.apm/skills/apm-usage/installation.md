# Installation

## Quick install (recommended)

```bash
# macOS / Linux
curl -sSL https://aka.ms/apm-unix | sh

# Windows (PowerShell)
irm https://aka.ms/apm-windows | iex
```

## Package managers

```bash
# Homebrew (macOS / Linux)
brew install microsoft/apm/apm

# Scoop (Windows)
scoop bucket add apm https://github.com/microsoft/scoop-apm
scoop install apm

# pip (all platforms, requires Python 3.10+)
pip install apm-cli
```

## Verify

```bash
apm --version
```

## Update

```bash
apm self-update          # update APM itself
apm self-update --check  # check for updates without installing
```

## Installer options (macOS / Linux)

```bash
# Specific version
curl -sSL https://aka.ms/apm-unix | sh -s -- @v1.2.3

# Custom install dir
curl -sSL https://aka.ms/apm-unix | APM_INSTALL_DIR=$HOME/.local/bin sh

# Air-gapped / GHE mirror - VERSION is required (skips GitHub API)
GITHUB_URL=https://github.corp.com VERSION=v1.2.3 sh install.sh
```

Unix binary installs require publisher `.sha256` sidecars, with no integrity-failure pip fallback or bypass. See [archive verification](https://github.com/microsoft/apm/blob/main/docs/src/content/docs/getting-started/installation.md#unix-archive-verification) for requirements and mirror migration, and [self-update](https://github.com/microsoft/apm/blob/main/docs/src/content/docs/reference/cli/self-update.md#enterprise-bootstrap-mirrors) for the release-tag installer caveat.

## Installer options (Windows PowerShell)

Uses the same variables as `install.sh` where applicable (`GITHUB_URL`, `APM_REPO`, `VERSION`, `APM_INSTALL_DIR`). See the full variable table, Actions example, checksum rules, and canonical Windows `PATH` layout in [installation.md](https://github.com/microsoft/apm/blob/main/docs/src/content/docs/getting-started/installation.md).

```powershell
# Pin a version (skips releases/latest API). Requires .sha256 on the release unless APM_SKIP_CHECKSUM=1 (emergency).
$env:VERSION = "v1.2.3"; irm https://aka.ms/apm-windows | iex

# Custom shim directory (contains apm.cmd; sibling current contains apm.exe)
$env:APM_INSTALL_DIR = "$env:LOCALAPPDATA\Programs\apm\bin"; irm https://aka.ms/apm-windows | iex

$env:GITHUB_URL = "https://github.corp.com"
$env:APM_REPO = "my-org/apm"
$env:VERSION = "v1.2.3"
irm https://aka.ms/apm-windows | iex
```

## Enterprise bootstrap mirrors

Use the variables below to install and update APM through an internal mirror. See the [installation bootstrap mirror section](https://github.com/microsoft/apm/blob/main/docs/src/content/docs/getting-started/installation.md#enterprise-bootstrap-mirror-mode) for fail-closed scope, GHES behavior, and the no-egress smoke test.

```bash
export APM_INSTALLER_BASE_URL="https://artifactory.mycorp.example/generic/apm-install"
export APM_RELEASE_METADATA_URL="https://artifactory.mycorp.example/generic/apm-releases/latest.json"
export APM_RELEASE_BASE_URL="https://artifactory.mycorp.example/generic/apm-releases"
export APM_PYPI_INDEX_URL="https://artifactory.mycorp.example/api/pypi/python-proxy/simple"
export APM_NO_DIRECT_FALLBACK=1
curl -sSL "$APM_INSTALLER_BASE_URL/install.sh" | sh
apm self-update --check
```

For dependency installs after bootstrap, keep using `PROXY_REGISTRY_URL` and `PROXY_REGISTRY_ONLY=1`. Homebrew and Scoop mirroring is package-manager documentation only in v0; these env vars do not rewrite Homebrew or Scoop internals.

## Troubleshooting

- **macOS/Linux "command not found":** ensure your install directory (default `/usr/local/bin`) is in `$PATH`.
- **Permission denied:** use `APM_INSTALL_DIR=$HOME/.local/bin` to install without sudo.
- **Windows antivirus locks:** set `$env:APM_DEBUG = "1"` and retry.
