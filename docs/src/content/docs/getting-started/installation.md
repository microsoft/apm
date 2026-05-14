---
title: "Installation"
description: "Install APM on macOS, Linux, Windows, or from source."
sidebar:
  order: 1
---

## Requirements

- macOS, Linux, or Windows (x86_64 or ARM64)
- [git](https://git-scm.com/) for dependency management
- Python 3.10+ (only for pip or from-source installs)

On **Windows ARM64**, the one-line installer currently downloads the **x86_64** ZIP (same as the GitHub Release asset); it runs via emulation. Native ARM64 Windows binaries are not selected yet.

## Quick install (recommended)

**macOS / Linux:**

```bash
curl -sSL https://aka.ms/apm-unix | sh
```

**Windows (PowerShell):**

```powershell
irm https://aka.ms/apm-windows | iex
```

The installer automatically detects your platform (macOS/Linux/Windows, Intel/ARM), downloads the latest binary, and adds `apm` to your `PATH`.

### Installer options

**macOS / Linux (`install.sh`):**

```bash
# Install a specific version
curl -sSL https://aka.ms/apm-unix | sh -s -- @v1.2.3

# Custom install directory
curl -sSL https://aka.ms/apm-unix | APM_INSTALL_DIR=$HOME/.local/bin sh

# Air-gapped / GitHub Enterprise mirror
GITHUB_URL=https://github.corp.com VERSION=v1.2.3 sh install.sh
```

**Windows (`install.ps1` in PowerShell):**

Air-gapped hosts should **save `install.ps1` locally** (the `irm` one-liner needs reachability to the script URL).

```powershell
# Pin a version (skips GitHub API - required for many air-gapped / GHES setups)
# Pinned installs verify SHA256 from the matching .sha256 unless you set:
#   $env:APM_SKIP_CHECKSUM = "1"   # emergency only
$env:VERSION = "v1.2.3"; irm https://aka.ms/apm-windows | iex

# Saved script: pass -SkipChecksum only when the release has no .sha256 sidecar (not recommended).
# .\install.ps1 v1.2.3 -SkipChecksum

# Custom directory for apm.cmd (default: %LOCALAPPDATA%\Programs\apm\bin)
$env:APM_INSTALL_DIR = "$env:LOCALAPPDATA\Programs\apm\bin"; irm https://aka.ms/apm-windows | iex

# Fork, enterprise host, or internal mirror (GITHUB_URL must be https://)
$env:GITHUB_URL = "https://github.corp.com"
$env:APM_REPO = "my-org/apm"
$env:VERSION = "v1.2.3"
irm https://aka.ms/apm-windows | iex
```

**GitHub Actions (`windows-latest`):**

```yaml
jobs:
  install-apm:
    runs-on: windows-latest
    steps:
      - name: Install APM (pinned, CI-safe)
        shell: pwsh
        env:
          VERSION: v0.13.0
          # For GHES or a mirror, set GITHUB_URL (https only) and APM_REPO as needed.
        run: |
          irm https://aka.ms/apm-windows | iex
          apm --version
      - uses: actions/checkout@v4
      - run: apm install --frozen
```

| Variable | Default | Description |
|----------|---------|-------------|
| `APM_INSTALL_DIR` | `/usr/local/bin` (Unix) / `%LOCALAPPDATA%\Programs\apm\bin` (Windows) | Directory for the `apm` symlink / `apm.cmd` shim |
| `APM_LIB_DIR` | `$(dirname APM_INSTALL_DIR)/lib/apm` | *(Unix only)* Directory for the full binary bundle |
| `GITHUB_URL` | `https://github.com` | Base GitHub URL (asset downloads **and** API host: `api.github.com` on github.com, `{GITHUB_URL}/api/v3` on GHES). Must be `https://`. |
| `APM_REPO` | `microsoft/apm` | Repository as `owner/name` |
| `VERSION` | *(latest)* | Pin a release tag (skips the **releases/latest** HTTP API). Must look like `v1.2.3` or `1.2.3`. |
| `APM_SKIP_CHECKSUM` | *(unset)* | Windows only: set to `1` to skip `.sha256` verification on **pinned** installs (emergency only). |

> **Note - Unix (`install.sh`):** Latest-release discovery still calls `https://api.github.com/repos/.../releases/latest` unless `VERSION` is set. For GHES or mirrors with no access to `api.github.com`, pin `VERSION` so the script never hits that endpoint.
>
> **Note - Windows (`install.ps1`):** The **releases/latest** URL is derived from `GITHUB_URL`: `https://api.github.com` for GitHub.com, or `{GITHUB_URL}/api/v3` for GitHub Enterprise Server. Air-gapped runners should still set `VERSION` so the installer does not need the API at all. When `VERSION` is pinned, the release **`.sha256`** file is required unless you set **`APM_SKIP_CHECKSUM=1`** (emergency only).

## Package managers

**Homebrew (macOS/Linux):**

```bash
brew install microsoft/apm/apm
```

**Scoop (Windows):**

```powershell
scoop bucket add apm https://github.com/microsoft/scoop-apm
scoop install apm
```

## pip install

```bash
pip install apm-cli
```

Requires Python 3.10+.

## Manual binary install

Download the archive for your platform from [GitHub Releases](https://github.com/microsoft/apm/releases/latest) and install manually:

#### Windows x86_64

```powershell
# Download and extract the Windows binary
Invoke-WebRequest -Uri https://github.com/microsoft/apm/releases/latest/download/apm-windows-x86_64.zip -OutFile apm-windows-x86_64.zip
Expand-Archive -Path .\apm-windows-x86_64.zip -DestinationPath .

# Copy to a permanent location and add to PATH
$installDir = "$env:LOCALAPPDATA\Programs\apm"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -Path .\apm-windows-x86_64\* -Destination $installDir -Recurse -Force
[Environment]::SetEnvironmentVariable("Path", "$installDir;" + [Environment]::GetEnvironmentVariable("Path", "User"), "User")
```

#### macOS / Linux
```bash
# Example: macOS Apple Silicon
curl -L https://github.com/microsoft/apm/releases/latest/download/apm-darwin-arm64.tar.gz | tar -xz
sudo mkdir -p /usr/local/lib/apm
sudo cp -r apm-darwin-arm64/* /usr/local/lib/apm/
sudo ln -sf /usr/local/lib/apm/apm /usr/local/bin/apm
```

Replace `apm-darwin-arm64` with the archive name for your macOS or Linux platform:

| Platform            | Archive name          |
|---------------------|-----------------------|
| macOS Apple Silicon | `apm-darwin-arm64`    |
| macOS Intel         | `apm-darwin-x86_64`   |
| Linux x86_64        | `apm-linux-x86_64`    |
| Linux ARM64         | `apm-linux-arm64`     |

## From source (contributors)

```bash
git clone https://github.com/microsoft/apm.git
cd apm

# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create environment and install in development mode
uv venv
uv pip install -e ".[dev]"
source .venv/bin/activate
```

## Build binary from source

To build a standalone binary with PyInstaller:

```bash
cd apm  # cloned repo from step above
uv pip install pyinstaller
chmod +x scripts/build-binary.sh
./scripts/build-binary.sh
```

The output binary is at `./dist/apm-{platform}-{arch}/apm`.

## Verify installation

```bash
apm --version
```

## Troubleshooting

### `apm: command not found` (macOS / Linux)

Ensure your install directory is in your `PATH`. The default is `/usr/local/bin`:

```bash
echo $PATH | tr ':' '\n' | grep /usr/local/bin
```

If missing, add it to your shell profile (`~/.zshrc`, `~/.bashrc`, etc.):

```bash
export PATH="/usr/local/bin:$PATH"
```

### Permission denied during install (macOS / Linux)

Use `sudo` for system-wide installation, or install to a user-writable directory:

```bash
curl -sSL https://aka.ms/apm-unix | APM_INSTALL_DIR=$HOME/.local/bin sh
```

### Binary install fails on older Linux (devcontainers, Debian-based images)

On systems with a glibc version older than the minimum required by the pre-built
binary (currently glibc 2.35), the binary will fail to run. The installer
automatically detects incompatible glibc versions and falls back to
`pip install --user apm-cli`.

This installs the `apm` command into your user `bin` directory (commonly `~/.local/bin`).
If `apm` is not found after installation, ensure that this directory is on your `PATH`.

**Recommended fix for devcontainers on very old base images:** switch to a base
image with glibc 2.35 or newer (e.g., the Debian `trixie` family, or
`mcr.microsoft.com/devcontainers/universal:24-trixie`), which runs the pre-built
binary directly without the pip fallback.

If you prefer to install via pip directly:

```bash
pip install --user apm-cli
```

### Authentication errors when installing packages

See [Authentication -- Troubleshooting](../authentication/#troubleshooting) for token setup, SSO authorization, and diagnosing auth failures.

### File access errors on Windows (antivirus / endpoint protection)

If `apm install` fails with `The process cannot access the file because it is being used by another process`, your antivirus or endpoint protection software is likely scanning temp files during installation.

APM retries file operations automatically with exponential backoff to handle transient locks. If the issue persists, set `APM_DEBUG=1` to see retry diagnostics:

```powershell
$env:APM_DEBUG = "1"
apm install <package>
```

## Next steps

See the [Quick Start](../quick-start/) to set up your first project.