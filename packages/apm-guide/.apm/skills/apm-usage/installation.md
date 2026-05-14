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
apm update          # update APM itself
apm update --check  # check for updates without installing
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

## Installer options (Windows PowerShell)

Uses the same variables as `install.sh` where applicable (`GITHUB_URL`, `APM_REPO`, `VERSION`, `APM_INSTALL_DIR`). See the full variable table, Actions example, and checksum rules in [installation.md](https://github.com/microsoft/apm/blob/main/docs/src/content/docs/getting-started/installation.md).

```powershell
# Pin a version (skips releases/latest API). Requires .sha256 on the release unless APM_SKIP_CHECKSUM=1 (emergency).
$env:VERSION = "v1.2.3"; irm https://aka.ms/apm-windows | iex

# Custom shim directory (directory that will contain apm.cmd)
$env:APM_INSTALL_DIR = "$env:LOCALAPPDATA\Programs\apm\bin"; irm https://aka.ms/apm-windows | iex

$env:GITHUB_URL = "https://github.corp.com"
$env:APM_REPO = "my-org/apm"
$env:VERSION = "v1.2.3"
irm https://aka.ms/apm-windows | iex
```

## Troubleshooting

- **macOS/Linux "command not found":** ensure your install directory (default `/usr/local/bin`) is in `$PATH`.
- **Permission denied:** use `APM_INSTALL_DIR=$HOME/.local/bin` to install without sudo.
- **Windows antivirus locks:** set `$env:APM_DEBUG = "1"` and retry.
