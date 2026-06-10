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

## Enterprise bootstrap mirrors

Use these env vars to install and update APM through an internal mirror and fail closed when a public fallback would be required:

| Variable | Purpose |
|----------|---------|
| `APM_INSTALLER_BASE_URL` | Base URL containing `install.sh` and `install.ps1`. |
| `APM_RELEASE_METADATA_URL` | Exact URL for mirrored `latest.json` release metadata. |
| `APM_RELEASE_BASE_URL` | Base URL for release assets at `{base}/{tag}/{asset}`. |
| `APM_PYPI_INDEX_URL` | PyPI proxy used by installer pip fallback. |
| `APM_NO_DIRECT_FALLBACK` | Set to `1` to block public GitHub, `aka.ms`, and PyPI fallback. |

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

No-egress smoke test: run the installer on a disposable runner with a curl wrapper or egress proxy that denies `github.com`, `api.github.com`, `aka.ms`, `pypi.org`, `pythonhosted.org`, Homebrew, and Scoop upstreams. With all mirror env vars set, the only allowed outbound host should be your mirror. Run `apm self-update --check` under the same env vars and confirm proxy logs show only the mirror host.

## Troubleshooting

- **macOS/Linux "command not found":** ensure your install directory (default `/usr/local/bin`) is in `$PATH`.
- **Permission denied:** use `APM_INSTALL_DIR=$HOME/.local/bin` to install without sudo.
- **Windows antivirus locks:** set `$env:APM_DEBUG = "1"` and retry.
