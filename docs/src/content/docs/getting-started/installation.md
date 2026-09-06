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

Fresh ordinary-user Unix installs use `~/.local/bin/apm` (launcher) and `~/.local/lib/apm` (bundle). The installer never runs `sudo` or edits profiles. If needed, update your current shell's `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Optionally add that line to your profile (`~/.zshrc`, `~/.bashrc`, etc.) yourself. Existing installs keep their [original destinations](#unix-install-ownership-and-migration).

On Windows, the installer adds both `current` and `bin` to `PATH`, with the stable `current\apm.exe` first. Bare `apm` calls therefore resolve the real executable in native shells, Git Bash, and process APIs such as Python `subprocess.run(["apm", ...])`; `bin\apm.cmd` remains available for compatibility.

### Installer options

**macOS / Linux (`install.sh`):**

```bash
# Install a specific version
curl -sSL https://aka.ms/apm-unix | sh -s -- @v1.2.3

# Custom directory for a fresh install (bundle: $HOME/tools/lib/apm)
curl -sSL https://aka.ms/apm-unix | APM_INSTALL_DIR="$HOME/tools/bin" sh

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

# Custom directory for apm.cmd (default: %LOCALAPPDATA%\Programs\apm\bin).
# The installer also adds the sibling current directory containing apm.exe.
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
| `APM_INSTALL_DIR` | `~/.local/bin` (fresh ordinary-user Unix install) / `%LOCALAPPDATA%\Programs\apm\bin` (Windows) | Unix launcher or Windows shim directory. Unix upgrades preserve the existing destination; overrides cannot redirect them. |
| `APM_LIB_DIR` | `~/.local/lib/apm` (fresh default Unix install) | Unix bundle; otherwise `lib/apm` under the install directory's parent, or preserved on upgrade. Must be absolute, end with `/apm`, and not be a symlink or shared directory. |
| `GITHUB_URL` | `https://github.com` | Base GitHub URL (asset downloads **and** API host: `api.github.com` on github.com, `{GITHUB_URL}/api/v3` on GHES). Must be `https://` on Windows. |
| `APM_REPO` | `microsoft/apm` | Repository as `owner/name` |
| `VERSION` | *(latest)* | Pin a release tag (skips the **releases/latest** HTTP API). Must look like `v1.2.3` or `1.2.3`. |
| `APM_RELEASE_METADATA_URL` | *(unset)* | Exact mirror URL for release metadata, usually `latest.json` with at least `{"tag_name":"vX.Y.Z"}`. |
| `APM_RELEASE_BASE_URL` | *(unset)* | Base URL for release assets laid out as `{base}/{tag}/{asset}` and `{base}/{tag}/{asset}.sha256`. |
| `APM_INSTALLER_BASE_URL` | *(unset)* | Base URL containing `install.sh` and `install.ps1`; used by `apm self-update` and by your bootstrap one-liner. |
| `APM_PYPI_INDEX_URL` | *(unset)* | PyPI-compatible mirror used when the installer falls back to pip. |
| `APM_NO_DIRECT_FALLBACK` | *(unset)* | Set to `1` to fail closed when a mirror is missing or unreachable instead of using public GitHub, `aka.ms`, or PyPI. |
| `APM_SKIP_CHECKSUM` | *(unset)* | Windows only: set to `1` to skip `.sha256` verification on **pinned** installs (emergency only). |

### Unix install ownership and migration

`install.sh` checks `PATH`, `$APM_INSTALL_DIR/apm`, `$APM_LIB_DIR/apm`, user-default destinations, historical `/usr/local/bin/apm`, `/opt/homebrew/bin/apm`, and `/usr/local/lib/apm/apm`, plus self-update's running binary. It recognizes a nonempty Unix bundle only when its bundle-identity entries are not symlinks: a regular `apm` file plus either a regular `.apm-installed` file or both a regular `VERSION` file and a real `_internal` directory. Discovery and pre-delete validation use this same rule; `VERSION` alone, `.apm-installed` alone, or `apm.cmd` alone never authorizes deletion. Recognized current and legacy layouts update in place, preserving their launcher and bundle destinations.

Preflight requires absolute, normalized launcher and bundle destinations; the bundle must end in `/apm`. It rejects blocked shared directories, destination overlaps, and an `APM_LIB_DIR` symlink before replacement. Root must set both `APM_INSTALL_DIR` and `APM_LIB_DIR`; setting only one refuses the install instead of filling the other from a default.

Before removal, every existing bundle entry must be caller-owned, and every directory must be writable and searchable by the caller. A failure stops the update without removing files. Conflicting installations or destination overrides, inaccessible discovery paths, unknown/package-managed launchers, and unwritable destinations also fail with repair guidance.

If a fresh install finds unrelated or unrecognized data, inspect it without deleting anything. Choose a different empty, dedicated bundle and launcher destination, or use the original owner's uninstall process.

After installing the bundle and launcher, `install.sh` runs the launcher at its destination with `--version` before reporting completion, even when it is off `PATH`; failure stops with guidance to retry the same destinations or ask the installation owner to repair it.

**Migration:** ask the original administrator or package manager to update system/custom installs. To migrate deliberately, uninstall through that owner first, then install fresh. Destination overrides never migrate an install. Automatic pip fallback requires a fresh ordinary-user install with neither destination variable set; existing/custom installs receive terminal owner guidance even when Python is unavailable.

Pip fallback uses the selected Python 3.10+ interpreter for both `-m pip` and its `sysconfig` user scheme. If that scheme query fails, installation stops before package installation. If the installed launcher is off `PATH`, the installer prints a safely shell-quoted command for the current shell using that interpreter's actual scripts directory, including `PYTHONUSERBASE` and framework layouts. It never modifies shell profiles.

After [saving and reviewing `install.sh`](#macos--linux), set both destinations. From an ordinary shell:

```bash
sudo env APM_INSTALL_DIR=/usr/local/bin APM_LIB_DIR=/usr/local/lib/apm sh ./install.sh
```

Already root or in a container without `sudo`:

```bash
env APM_INSTALL_DIR=/usr/local/bin APM_LIB_DIR=/usr/local/lib/apm sh ./install.sh
```

Root without both variables fails closed. The installer never elevates privileges.

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

Unix release assets are `apm-linux-x86_64.tar.gz`, `apm-linux-arm64.tar.gz`, `apm-darwin-x86_64.tar.gz`, and `apm-darwin-arm64.tar.gz`.

`APM_NO_DIRECT_FALLBACK=1` makes missing mirror settings and unreachable mirrors hard failures. It does not replace package-install proxying; keep using `PROXY_REGISTRY_URL` and `PROXY_REGISTRY_ONLY=1` for `apm install` dependencies.

#### What fail-closed does and does not cover

Fail-closed scoping keys off the public `github.com` default. The guard only blocks egress when the resolved host would be public GitHub (`github.com` / `api.github.com`), `aka.ms`, or public PyPI. It does **not** suppress egress to a custom `GITHUB_URL`: if you set a GHES host (for example `GITHUB_URL=https://github.corp.com`) together with `APM_NO_DIRECT_FALLBACK=1` and no release mirror, the installer still reaches that GHES host. This is intentional coexistence with GHES, but "no direct fallback" should not be read as "zero egress" -- it means "no fallback to public hosts". For true zero-egress, set the `APM_RELEASE_METADATA_URL` / `APM_RELEASE_BASE_URL` / `APM_INSTALLER_BASE_URL` / `APM_PYPI_INDEX_URL` mirrors so every request resolves to your internal hosts. When `APM_RELEASE_METADATA_URL` is unset, GHES metadata requests intentionally use the resolved GitHub token for that host; mirror metadata requests never receive it. The GitHub token is attached only when the request targets the canonical GitHub / configured GHES host, never a mirror host.

Homebrew and Scoop mirror support is docs-only in this v0: mirror the tap or bucket with your package manager's normal enterprise controls, but the APM env vars above do not rewrite Homebrew or Scoop internals.

### No-egress smoke test

Run as an ordinary user on a disposable Linux or macOS runner with no existing APM installation. This starts a local mirror, denies public hosts through `curl`/`pip` wrappers, and expects failure after downloading the fake archive. The wrappers reject public fallback; pip also requires `APM_PYPI_INDEX_URL` in fail-closed mode.

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
python3 -m pip install apm-cli
```

Requires Python 3.10+. Update or uninstall with the Python interpreter that owns the installation:

```bash
python3 -m pip install --upgrade apm-cli
python3 -m pip uninstall apm-cli
```

Replace `python3` with the owning interpreter when needed. Uninstall before switching to the binary installer; `install.sh` refuses package-managed launchers.

## Manual binary install

For Windows, download the archive from [GitHub Releases](https://github.com/microsoft/apm/releases/latest). For Unix, save the installer below.

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

Save and review the installer, then run it to retain [ownership checks](#unix-install-ownership-and-migration):

```bash
curl -fsSL https://aka.ms/apm-unix -o install.sh
# Review ./install.sh before running it.
sh ./install.sh
```

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

Run the POSIX-quoted `export PATH=...` command printed by the installer. For native installation destinations containing `:` or control characters, use the printed absolute-path command instead.

### Permission denied during install (macOS / Linux)

For existing installations, follow [ownership and migration](#unix-install-ownership-and-migration). Fresh destinations must be writable, absolute paths without dot segments.

### Binary install fails on older Linux (devcontainers, Debian-based images)

Prebuilt Linux binaries require glibc 2.35+. Use a compatible base image (for example, `mcr.microsoft.com/devcontainers/universal:24-trixie`) or Python 3.10+ and pip. Automatic `pip install --user apm-cli` fallback follows the [ownership rules](#unix-install-ownership-and-migration). Update or uninstall a fallback install with the same Python interpreter's `-m pip`; uninstall it before switching to the binary installer.

### Authentication errors when installing packages

See [Authentication -- Troubleshooting](../authentication/#troubleshooting) for token setup, SSO authorization, and diagnosing auth failures.

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