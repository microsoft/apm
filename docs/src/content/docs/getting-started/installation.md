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
| `APM_LIB_DIR` | `$(dirname APM_INSTALL_DIR)/lib/apm` | *(Unix only)* Directory for the full binary bundle. Must end with `/apm` (for example, `/lib/apm`). The installer rejects shared directories (e.g. `$HOME/.local/share`) to prevent accidental data loss. |
| `GITHUB_URL` | `https://github.com` | Base GitHub URL (asset downloads **and** API host: `api.github.com` on github.com, `{GITHUB_URL}/api/v3` on GHES). Must be `https://` on Windows. |
| `APM_REPO` | `microsoft/apm` | Repository as `owner/name` |
| `VERSION` | *(latest)* | Pin a release tag (skips the **releases/latest** HTTP API). Must look like `v1.2.3` or `1.2.3`. |
| `APM_RELEASE_METADATA_URL` | *(unset)* | Exact mirror URL for release metadata, usually `latest.json` with at least `{"tag_name":"vX.Y.Z"}`. |
| `APM_RELEASE_BASE_URL` | *(unset)* | Base URL for release assets laid out as `{base}/{tag}/{asset}` and `{base}/{tag}/{asset}.sha256`. |
| `APM_INSTALLER_BASE_URL` | *(unset)* | Base URL containing `install.sh` and `install.ps1`; used by `apm self-update` and by your bootstrap one-liner. |
| `APM_PYPI_INDEX_URL` | *(unset)* | PyPI-compatible mirror used when the installer falls back to pip. |
| `APM_NO_DIRECT_FALLBACK` | *(unset)* | Set to `1` to fail closed when a mirror is missing or unreachable instead of using public GitHub, `aka.ms`, or PyPI. |
| `APM_SKIP_CHECKSUM` | *(unset)* | Windows only: set to `1` to skip `.sha256` verification on **pinned** installs (emergency only). |

### Enterprise bootstrap mirror mode

Mirror mode routes bootstrap traffic through internal hosts. Four URL variables point install and self-update at your mirror; `APM_NO_DIRECT_FALLBACK=1` fails closed so no request reaches a public host:

```bash
export APM_INSTALLER_BASE_URL="https://artifactory.mycorp.example/generic/apm-install"
export APM_RELEASE_METADATA_URL="https://artifactory.mycorp.example/generic/apm-releases/latest.json"
export APM_RELEASE_BASE_URL="https://artifactory.mycorp.example/generic/apm-releases"
export APM_PYPI_INDEX_URL="https://artifactory.mycorp.example/api/pypi/python-proxy/simple"
export APM_NO_DIRECT_FALLBACK=1

curl -sSL "$APM_INSTALLER_BASE_URL/install.sh" | sh
apm self-update --check
```

For Windows:

```powershell
$env:APM_INSTALLER_BASE_URL = "https://artifactory.mycorp.example/generic/apm-install"
$env:APM_RELEASE_METADATA_URL = "https://artifactory.mycorp.example/generic/apm-releases/latest.json"
$env:APM_RELEASE_BASE_URL = "https://artifactory.mycorp.example/generic/apm-releases"
$env:APM_PYPI_INDEX_URL = "https://artifactory.mycorp.example/api/pypi/python-proxy/simple"
$env:APM_NO_DIRECT_FALLBACK = "1"

irm "$env:APM_INSTALLER_BASE_URL/install.ps1" | iex
apm self-update --check
```

Mirror layout for binary releases:

```text
apm-releases/
  latest.json
  v0.19.0/
    apm-linux-x86_64.tar.gz
    apm-darwin-arm64.tar.gz
    apm-windows-x86_64.zip
    apm-windows-x86_64.zip.sha256
```

`APM_NO_DIRECT_FALLBACK=1` makes missing mirror settings and unreachable mirrors hard failures. It does not replace package-install proxying; keep using `PROXY_REGISTRY_URL` and `PROXY_REGISTRY_ONLY=1` for `apm install` dependencies.

#### What fail-closed does and does not cover

Fail-closed scoping keys off the public `github.com` default. The guard only blocks egress when the resolved host would be public GitHub (`github.com` / `api.github.com`), `aka.ms`, or public PyPI. It does **not** suppress egress to a custom `GITHUB_URL`: if you set a GHES host (for example `GITHUB_URL=https://github.corp.com`) together with `APM_NO_DIRECT_FALLBACK=1` and no release mirror, the installer still reaches that GHES host. This is intentional coexistence with GHES, but "no direct fallback" should not be read as "zero egress" -- it means "no fallback to public hosts". For true zero-egress, set the `APM_RELEASE_METADATA_URL` / `APM_RELEASE_BASE_URL` / `APM_INSTALLER_BASE_URL` / `APM_PYPI_INDEX_URL` mirrors so every request resolves to your internal hosts. When `APM_RELEASE_METADATA_URL` is unset, GHES metadata requests intentionally use the resolved GitHub token for that host; mirror metadata requests never receive it. The GitHub token is attached only when the request targets the canonical GitHub / configured GHES host, never a mirror host.

Homebrew and Scoop mirror support is docs-only in this v0: mirror the tap or bucket with your package manager's normal enterprise controls, but the APM env vars above do not rewrite Homebrew or Scoop internals.

### No-egress smoke test

Run this on a disposable Linux or macOS runner. It starts a local mirror, wraps both `curl` and `pip` with deny-lists for public hosts, and expects the installer to fail only after downloading the fake archive. Any request to GitHub, `aka.ms`, PyPI, Homebrew, or Scoop fails the smoke test immediately. The `pip` wrapper makes the PyPI egress path explicit: pip fallback is gated by `APM_NO_DIRECT_FALLBACK` + `APM_PYPI_INDEX_URL`, so the wrapper proves pip cannot reach public PyPI even if the binary path falls back to it.

```bash
set -eu
rm -rf .apm-mirror-smoke
mkdir -p .apm-mirror-smoke/mirror/apm-install \
  .apm-mirror-smoke/mirror/apm-releases/v9.9.9 \
  .apm-mirror-smoke/bin
cp install.sh .apm-mirror-smoke/mirror/apm-install/install.sh
printf '{"tag_name":"v9.9.9"}\n' > .apm-mirror-smoke/mirror/apm-releases/latest.json
printf 'not-a-real-archive\n' > .apm-mirror-smoke/mirror/apm-releases/v9.9.9/apm-linux-x86_64.tar.gz
cat > .apm-mirror-smoke/bin/curl <<'SH'
#!/bin/sh
case " $* " in
  *github.com*|*api.github.com*|*aka.ms*|*pypi.org*|*pythonhosted.org*|*brew.sh*|*scoop*)
    echo "public egress blocked: $*" >&2
    exit 70
    ;;
esac
exec /usr/bin/curl "$@"
SH
cat > .apm-mirror-smoke/bin/pip <<'SH'
#!/bin/sh
# Deny public PyPI; allow only the configured mirror index.
case " $* " in
  *pypi.org*|*pythonhosted.org*)
    echo "public pip egress blocked: $*" >&2
    exit 70
    ;;
esac
exec /usr/bin/pip "$@"
SH
cp .apm-mirror-smoke/bin/pip .apm-mirror-smoke/bin/pip3
chmod +x .apm-mirror-smoke/bin/curl .apm-mirror-smoke/bin/pip .apm-mirror-smoke/bin/pip3
python3 -m http.server 8765 --directory .apm-mirror-smoke/mirror > .apm-mirror-smoke/server.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT
set +e
PATH="$PWD/.apm-mirror-smoke/bin:$PATH" \
  APM_INSTALLER_BASE_URL="http://127.0.0.1:8765/apm-install" \
  APM_RELEASE_METADATA_URL="http://127.0.0.1:8765/apm-releases/latest.json" \
  APM_RELEASE_BASE_URL="http://127.0.0.1:8765/apm-releases" \
  APM_PYPI_INDEX_URL="http://127.0.0.1:8765/pypi/simple" \
  APM_NO_DIRECT_FALLBACK=1 \
  sh .apm-mirror-smoke/mirror/apm-install/install.sh
status=$?
set -e
test "$status" -ne 0
# Success: installer failed closed with an actionable error, as expected.
```

For `apm self-update`, run `apm self-update --check` with the same env vars and verify your proxy, firewall, or CI egress logs show only the mirror host. Use a disposable runner for a full `apm self-update` because it executes the mirrored installer.

## Package managers

**Node.js projects:**

```bash
# Global installation
npm install -g @microsoft/apm-cli
# or: pnpm add -g @microsoft/apm-cli
# or: yarn global add @microsoft/apm-cli

# Or as a project devDependency (recommended)
npm install -D @microsoft/apm-cli
# or: pnpm add -D @microsoft/apm-cli
# or: yarn add -D @microsoft/apm-cli

npx apm --version
```

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

> **Important:** The installer validates `APM_LIB_DIR` before writing any files.
> See the `APM_LIB_DIR` row above for path rules. The installer also refuses to
> delete an existing non-empty `APM_LIB_DIR` unless it looks like a prior APM
> install. When you set `APM_INSTALL_DIR=$HOME/.local/bin`, the derived lib path
> (`$HOME/.local/lib/apm`) is safe and does not require sudo.

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

See [Authentication -- Troubleshooting](./authentication/#troubleshooting) for token setup, SSO authorization, and diagnosing auth failures.

### File access errors on Windows (antivirus / endpoint protection)

If `apm install` fails with `The process cannot access the file because it is being used by another process`, your antivirus or endpoint protection software is likely scanning temp files during installation.

APM retries file operations automatically with exponential backoff to handle transient locks. If the issue persists, set `APM_DEBUG=1` to see retry diagnostics:

```powershell
$env:APM_DEBUG = "1"
apm install <package>
```

### `Access is denied` running apm.exe on Windows (AppLocker / App Control for Business)

If the installer (or `apm self-update`) fails at the `Testing binary...` step with `Access is denied` / HRESULT `0x80070005`, an enterprise application control policy ([AppLocker](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/applocker-overview) or [App Control for Business / WDAC](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/)) is blocking execution of `apm.exe` from a user-writable path.

The installer stages the binary under `%LOCALAPPDATA%\Programs\apm\releases\<tag>` **before** invoking it, so a single allow-list rule for that path is enough.

Ask your endpoint admin to add one of:

- **Path rule:** `%LOCALAPPDATA%\Programs\apm\*`
- **Publisher / hash rule** for the released `apm.exe`

If you cannot change policy, set `APM_TEMP_DIR` to a directory your policy allows and retry:

```powershell
$env:APM_TEMP_DIR = "$env:LOCALAPPDATA\Programs\apm\tmp"
irm https://aka.ms/apm-windows | iex
```

As a last resort, install via pip (runs from your Python user site):

```powershell
pip install --user apm-cli
```

## Next steps

See the [Quickstart](/apm/quickstart/) to set up your first project.