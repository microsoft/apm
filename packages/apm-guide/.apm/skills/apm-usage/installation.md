# Installation

## Quick install (recommended)

```bash
# macOS / Linux
curl -sSL https://aka.ms/apm-unix | sh

# Windows (PowerShell)
irm https://aka.ms/apm-windows | iex
```

Fresh ordinary-user Unix installs use `~/.local/bin/apm` and `~/.local/lib/apm`, without `sudo` or profile edits. Set current-shell `PATH` if needed; optionally add this to your profile yourself:

```bash
export PATH="$HOME/.local/bin:$PATH"
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

# Custom fresh install (bundle: $HOME/tools/lib/apm)
curl -sSL https://aka.ms/apm-unix | APM_INSTALL_DIR="$HOME/tools/bin" sh

# Air-gapped / GHE mirror - VERSION is required (skips GitHub API)
GITHUB_URL=https://github.corp.com VERSION=v1.2.3 sh install.sh
```

### Unix ownership and migration

`install.sh` preserves recognized installations and refuses shadow installs, package-managed or unrecognized launchers, foreign-owned bundles, unsafe destinations, and destination changes.

Inspect unrecognized data without deleting it. For a fresh install, choose another empty dedicated bundle; otherwise use the original owner's uninstall process.

Update or uninstall pip-owned installs with the owning Python's `-m pip`; uninstall before switching to the binary installer. Automatic pip fallback requires a fresh ordinary-user install with neither destination variable set. It prints that Python's user scripts directory when off `PATH` and never edits profiles.

Root requires both `APM_INSTALL_DIR` and `APM_LIB_DIR`; one variable refuses the install. See the canonical [Unix ownership and migration procedure](https://microsoft.github.io/apm/getting-started/installation/#unix-install-ownership-and-migration).

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

Set `APM_INSTALLER_BASE_URL`, `APM_RELEASE_METADATA_URL`, `APM_RELEASE_BASE_URL`, `APM_PYPI_INDEX_URL`, and `APM_NO_DIRECT_FALLBACK=1` to install and update APM through an internal mirror while failing closed on public fallback. For verification, run the installer and `apm self-update --check` behind an egress proxy or wrappers that deny public GitHub, `aka.ms`, PyPI, Homebrew, and Scoop; only your mirror host should appear. The canonical setup, GHES scoping note, and full no-egress smoke recipe live in the [installation bootstrap mirror section](https://github.com/microsoft/apm/blob/main/docs/src/content/docs/getting-started/installation.md#enterprise-bootstrap-mirror-mode).

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

- **macOS/Linux "command not found":** run the printed POSIX-quoted PATH command; for native installation destinations containing `:` or control characters, use the printed absolute-path command.
- **Permission denied:** follow [ownership and migration](#unix-ownership-and-migration), not a destination override.
- **Windows antivirus locks:** set `$env:APM_DEBUG = "1"` and retry.
