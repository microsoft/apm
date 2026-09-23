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

## Homebrew (macOS/Linux)

Already use Homebrew on macOS? Install from Homebrew core (also available on Linux):

```bash
brew install apm
```

No custom tap needed. Homebrew manages the APM CLI installation and updates.
Package-manager ownership does not eliminate supply-chain risk.
Use `brew upgrade apm`, not `apm self-update`; see the
[Homebrew core update policy](../../reference/cli/self-update/#description).

Homebrew is optional. Use the standalone installer below, [pip](#pip-install),
[WinGet or Scoop](#package-managers), or a [manual binary install](#manual-binary-install).

Already installed from `microsoft/apm/apm`? See
[Migrate from the Microsoft tap](#migrate-from-the-microsoft-tap).

## Standalone installer

**Linux / macOS without Homebrew:**

```bash
curl -sSL https://aka.ms/apm-unix | sh
```

**Windows (PowerShell):**

```powershell
irm https://aka.ms/apm-windows | iex
```

Fresh ordinary-user Unix native installs use `~/.local/bin/apm` (launcher) and `~/.local/lib/apm` (bundle). The installer never runs `sudo`. For bash, zsh, and fish, it automatically configures future shells when the install path and profile files are safe. Open a new shell after install.

If the installer skips profile edits, it prints an exact command for the current shell. You can also activate the default install manually:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

For fish:

```fish
set -gx PATH "$HOME/.local/bin" $PATH
```

Existing installs keep their [original destinations](#unix-install-ownership-and-migration).

On Windows, the installer adds both `current` and `bin` to `PATH`, with the stable `current\apm.exe` first. Bare `apm` calls therefore resolve the real executable in native shells, Git Bash, and process APIs such as Python `subprocess.run(["apm", ...])`; `bin\apm.cmd` remains available for compatibility.

### Public release metadata

CLI bootstrap and update checks are separate from [package authentication](../authentication/). Installers query metadata authenticated-first when a token is set: `GITHUB_APM_PAT` -> `GITHUB_TOKEN` -> `GH_TOKEN`. Python checks and stable/prerelease `apm self-update` use AuthResolver's environment-only chain, including per-org tokens before global variables, without invoking `gh` or Git credential helpers.

An authenticated 401 or non-rate-limit 403 permits one anonymous retry, only at `https://api.github.com/repos/microsoft/apm/releases/latest` or `https://api.github.com/repos/microsoft/apm/releases?per_page=5` (prereleases). Primary/secondary rate limits (403 identified by headers or documented messages, or 429), network errors, other HTTP failures, and malformed metadata never trigger this retry.

Anonymous recovery excludes private/custom repositories, GHES/custom hosts, explicit `APM_RELEASE_METADATA_URL` (even a canonical public URL), and `APM_NO_DIRECT_FALLBACK=1`. Mirrors never receive GitHub tokens or fall back to public URLs. `VERSION` skips latest-release discovery; a private Unix checksum retry can still query the selected tag.

PowerShell metadata requests pass explicit headers and exclude `Credential`, `UseDefaultCredentials`, `Authentication`, `Token`, and `WebSession` defaults from a function-local copy of `$PSDefaultParameterValues`, retaining caller proxy configuration.

See [self-update errors](../../reference/cli/self-update/#failure-modes) for diagnostics; background checks stay quiet.

### Installer options

**macOS / Linux (`install.sh`):**

```bash
# Install a specific version
curl -sSL https://aka.ms/apm-unix | sh -s -- @v1.2.3

# Install under one root (launcher: $HOME/.local/bin, bundle: $HOME/.local/lib/apm)
curl -sSL https://aka.ms/apm-unix | sh -s -- --prefix "$HOME/.local"

# Custom directory for a fresh install (bundle: $HOME/tools/lib/apm)
curl -sSL https://aka.ms/apm-unix | APM_INSTALL_DIR="$HOME/tools/bin" sh

# Opt out of automatic shell PATH setup
curl -sSL https://aka.ms/apm-unix | APM_NO_MODIFY_PATH=1 sh

# GHES release host (not a generic air-gap mirror). VERSION skips
# releases/latest; private checksum retries can still query the exact tag.
GITHUB_URL=https://github.corp.com VERSION=v1.2.3 sh install.sh
```

**Windows (`install.ps1` in PowerShell):**

Air-gapped hosts should **save `install.ps1` locally** (the `irm` one-liner needs reachability to the script URL).

```powershell
# Pin a version (skips releases/latest - required for many air-gapped / GHES setups)
# Pinned installs verify SHA-256 from the matching .sha256 unless you set:
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
| `--prefix PATH` | *(unset)* | Unix `install.sh` option. Selects one root and derives `PATH/bin` for the launcher plus `PATH/lib/apm` for the bundle. Accepts both `--prefix PATH` and `--prefix=PATH`; the value must be an absolute, normalized path. Counts as explicitly setting both Unix destinations for administrator-run installs. |
| `APM_INSTALL_DIR` | `~/.local/bin` (fresh ordinary-user Unix install) / `%LOCALAPPDATA%\Programs\apm\bin` (Windows) | Unix launcher or Windows shim directory. Unix upgrades preserve the existing destination; overrides cannot redirect them. When `--prefix` is present on Unix, this may be set only to the matching derived `PATH/bin`. |
| `APM_LIB_DIR` | `~/.local/lib/apm` (fresh default Unix install) | Unix bundle; otherwise `lib/apm` under the install directory's parent, or preserved on upgrade. Must be absolute, end with `/apm`, and not be a symlink or shared directory. When `--prefix` is present on Unix, this may be set only to the matching derived `PATH/lib/apm`. |
| `APM_NO_MODIFY_PATH` | *(unset)* | Unix native installer only. Set a truthy value to skip shell profile edits and persist that disabled preference after a successful native install. Unset inherits a disabled preference from the native receipt. Set `APM_NO_MODIFY_PATH=0` on a normal desktop rerun to re-enable automatic shell PATH setup. Pip fallback, CI/headless/root installs, self-update, unknown shells, unsafe profile files, and unsafe install paths never edit profiles. |
| `GITHUB_URL` | `https://github.com` | Base GitHub URL (asset downloads **and** API host: `api.github.com` on github.com, `{GITHUB_URL}/api/v3` on GHES). Must be `https://` on Windows. |
| `APM_REPO` | `microsoft/apm` | Repository as `owner/name` |
| `VERSION` | *(latest)* | Pin a release tag (skips the **releases/latest** HTTP API). Must look like `v1.2.3` or `1.2.3`. |
| `APM_RELEASE_METADATA_URL` | *(unset)* | Exact mirror URL for release metadata, usually `latest.json` with at least `{"tag_name":"vX.Y.Z"}`. |
| `APM_RELEASE_BASE_URL` | *(unset)* | Base URL for release assets laid out as `{base}/{tag}/{asset}` and `{base}/{tag}/{asset}.sha256`. |
| `APM_INSTALLER_BASE_URL` | *(unset)* | Base URL containing `install.sh` and `install.ps1`; used by `apm self-update` and by your bootstrap one-liner. |
| `APM_PYPI_INDEX_URL` | *(unset)* | PyPI-compatible mirror used when the installer falls back to pip. |
| `APM_NO_DIRECT_FALLBACK` | *(unset)* | Set to `1` to fail closed when a mirror is missing or unreachable instead of using public GitHub, `aka.ms`, or PyPI. |
| `APM_SKIP_CHECKSUM` | *(unset)* | Windows only: set to `1` to skip `.sha256` verification on **pinned** installs (emergency only). |

### Unix archive verification

For every selected Unix binary release, `install.sh` fetches `{tag}/{archive}.sha256` through the same release-asset route and mirror as the archive. Canonical GitHub/GHES release-asset API retries are scoped to the configured host and numeric asset IDs; mirrors receive no GitHub auth.

The installer parses the sidecar, checks hash-tool availability, and requires one record -- 64 hex SHA-256 characters, two spaces (or space plus `*`), then the exact archive basename -- before downloading the archive.

Before extraction or execution, the installer compares the archive hash using `sha256sum` or `shasum -a 256`. Missing, malformed, unreachable, or mismatching checksums, or unavailable/failed hashing, stop installation. Integrity failures have no pip fallback or bypass flag.

Historical releases and custom mirrors without sidecars are refused. Older tagged installer scripts are not retroactively patched; self-update inherits archive verification only when the selected installer includes the guard. Upgrade the mirrored installer and publish matching original publisher sidecars beside the archives, or select a release with sidecars. Do not generate replacement checksums from untrusted downloads.

This checks integrity against the same publisher's checksum, not independent provenance or a signature.

### Unix install ownership and migration

`install.sh` checks `PATH`, `$APM_INSTALL_DIR/apm`, `$APM_LIB_DIR/apm`, user-default destinations, historical `/usr/local/bin/apm`, `/opt/homebrew/bin/apm`, and `/usr/local/lib/apm/apm`, plus self-update's running binary. It recognizes a nonempty Unix bundle only when its bundle-identity entries are not symlinks: a regular `apm` file plus either a regular `.apm-installed` file or both a regular `VERSION` file and a real `_internal` directory. Discovery and pre-delete validation use this same rule; `VERSION` alone, `.apm-installed` alone, or `apm.cmd` alone never authorizes deletion. Recognized current and legacy layouts update in place, preserving their launcher and bundle destinations.

Preflight requires absolute, normalized launcher and bundle destinations; the bundle must end in `/apm`. It rejects blocked shared directories, destination overlaps, and an `APM_LIB_DIR` symlink before replacement. Root must set both `APM_INSTALL_DIR` and `APM_LIB_DIR`; setting only one refuses the install instead of filling the other from a default.

`--prefix PATH` is a destination selector, not a privilege flag. It derives `PATH/bin` and `PATH/lib/apm` in the same ownership/path-selection authority as the environment variables. Contradictory `APM_INSTALL_DIR` or `APM_LIB_DIR` values fail before download or writes; redundant matching values are accepted. The installer never runs `sudo`.

Before removal, every existing bundle entry must be caller-owned, and every directory must be writable and searchable by the caller. A failure stops the update without removing files. Conflicting installations or destination overrides, inaccessible discovery paths, unknown/package-managed launchers, and unwritable destinations also fail with repair guidance.

If a fresh install finds unrelated or unrecognized data, inspect it without deleting anything. Choose a different empty, dedicated bundle and launcher destination, or use the original owner's uninstall process.

After installing the bundle and launcher, `install.sh` runs the launcher at its destination with `--version` before reporting completion, even when it is off `PATH`; failure stops with guidance to retry the same destinations or ask the installation owner to repair it.

**Migration:** ask the original administrator or package manager to update system/custom installs. To migrate deliberately, uninstall through that owner first, then install fresh. Destination overrides never migrate an install. Automatic pip fallback requires a fresh ordinary-user install with neither destination variable set; existing/custom installs receive terminal owner guidance even when Python is unavailable.

Pip fallback uses the selected Python 3.10+ interpreter for both `-m pip` and its `sysconfig` user scheme. If that scheme query fails, installation stops before package installation. If the installed launcher is off `PATH`, the installer prints a safely shell-quoted command for the current shell using that interpreter's actual scripts directory, including `PYTHONUSERBASE` and framework layouts. Pip fallback never modifies shell profiles or writes native shell setup policy.

For native installs, automatic shell setup writes only installer-owned marked profile blocks that source generated hooks under `~/.apm/shell`. Truthy `APM_NO_MODIFY_PATH` writes no profile or hook changes and records the disabled preference only after the native binary succeeds. To re-enable after opting out, rerun a normal desktop native install with `APM_NO_MODIFY_PATH=0`. During `apm self-update`, the installer never enrolls shell setup or edits profiles/hooks.

After [saving and reviewing `install.sh`](#macos--linux), select the system root explicitly. From an ordinary shell:

```bash
sudo sh ./install.sh --prefix /usr/local
```

Already root or in a container without `sudo`:

```bash
sh ./install.sh --prefix /usr/local
```

Root without both destinations, or without one `--prefix`, fails closed. The installer never elevates privileges.

### Enterprise bootstrap mirror mode

Set these four mirror URLs for CLI install and self-update; `APM_NO_DIRECT_FALLBACK=1` blocks public fallback. Release metadata does not follow redirects: set `APM_RELEASE_METADATA_URL` to the final JSON endpoint, or pin `VERSION`.

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
    apm-linux-x86_64.tar.gz.sha256
    apm-darwin-arm64.tar.gz
    apm-darwin-arm64.tar.gz.sha256
    apm-windows-x86_64.zip
    apm-windows-x86_64.zip.sha256
```

Unix release assets are `apm-linux-x86_64.tar.gz`, `apm-linux-arm64.tar.gz`, `apm-darwin-x86_64.tar.gz`, and `apm-darwin-arm64.tar.gz`.

`APM_NO_DIRECT_FALLBACK=1` makes missing mirror settings and unreachable mirrors hard failures. It does not replace package-install proxying; keep using `PROXY_REGISTRY_URL` and `PROXY_REGISTRY_ONLY=1` for `apm install` dependencies.

Mirror-mode installs follow the same Unix shell PATH rules as normal native installs.

#### What fail-closed does and does not cover

`APM_NO_DIRECT_FALLBACK=1` blocks public GitHub (`github.com` / `api.github.com`), `aka.ms`, and public PyPI, not a custom `GITHUB_URL`. With `GITHUB_URL=https://github.corp.com` and no metadata mirror, the installer still contacts GHES using its resolved GitHub token. "No direct fallback" means no fallback to public hosts, not zero egress. To keep all requests internal, configure all four mirrors above. See [Public release metadata](#public-release-metadata) for mirror token isolation and retry restrictions.

Homebrew and Scoop mirror support is docs-only in this v0: mirror the tap or bucket with your package manager's normal enterprise controls, but the APM env vars above do not rewrite Homebrew or Scoop internals.

### No-egress smoke test

Run as an ordinary user on a disposable Linux or macOS runner with no existing APM installation. This starts a local mirror and wraps `curl` and `pip` to reject public hosts.

This fixture fails at checksum fetch because it has no `.sha256`, before archive download or extraction and without pip fallback. The wrappers reject public fallback; pip also requires `APM_PYPI_INDEX_URL` in fail-closed mode.

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

**Homebrew (macOS/Linux):** See [Homebrew](#homebrew-macoslinux) above.

**WinGet (Windows):**

Requires [WinGet / App Installer](https://learn.microsoft.com/en-us/windows/package-manager/winget/#install-winget) on Windows 10 version 1809 or later.

```powershell
winget install --id Microsoft.APM --exact --source winget
```

Update with `winget upgrade --id Microsoft.APM --exact --source winget`, not `apm self-update`.

**Scoop (Windows):**

```powershell
scoop bucket add apm https://github.com/microsoft/scoop-apm
scoop install apm
```

### Migrate from the Microsoft tap

This procedure covers a linked, unpinned `microsoft/apm/apm` installation in the
current Homebrew prefix. It does not cover unlinked kegs, multiple prefixes, or
standalone installs.

1. Inspect the installation before changing it:

   ```bash
   type -a apm
   brew --prefix
   brew list --formula --full-name
   brew list --pinned
   ls -l "$(brew --prefix)/bin/apm"
   ```

   Confirm the list contains `microsoft/apm/apm`, APM is not pinned, and your
   shell resolves `apm` to that prefix's `bin/apm`, linked to its Homebrew-managed
   APM keg. If another installation owns or shadows the path, stop and resolve
   ownership separately. Back up
   `~/.apm/config.json` using your normal process.

2. Compare the installed version with core's available version:

   ```bash
   brew list --versions apm
   brew info --formula homebrew/core/apm
   ```

   :::caution[Stop if core is older]
   Reinstall can downgrade without a separate warning. If core's version is
   older than your installed version, stop and wait for core to catch up.
   An older APM may not understand your configuration; preserving the file's
   bytes does not prove runtime compatibility.
   :::

3. Reinstall with the explicit core name:

   ```bash
   brew reinstall homebrew/core/apm
   ```

   Unqualified `brew reinstall apm` stays on the tap. No preliminary uninstall
   or tap removal is needed.

4. Verify the receipt and active command:

   ```bash
   brew list --formula --full-name
   brew --prefix
   command -v apm
   type -a apm
   apm --version
   ```

   The formula list must now show `apm`, not `microsoft/apm/apm`, and the shell
   must resolve to the intended prefix's `bin/apm`. A successful reinstall alone
   does not rule out another copy earlier on `PATH`.

The migration leaves `~/.apm/config.json` unchanged on disk. Update through
`brew upgrade apm`.

:::caution[Link conflict is not rollback]
If linking fails on a conflicting file or symlink, core is already installed
but unlinked; the tap installation is not restored. The conflicting file is
preserved. Stop and do not use `--overwrite`. Have the file's owner resolve the
conflict, then run `brew link homebrew/core/apm` and repeat step 4.
:::

:::note[Verification scope]
Homebrew 6.0.22 (`29b882c`) was exercised on macOS with isolated fixture bottles,
covering same-version replacement, upgrades, downgrades, and link conflicts. The pinned
[tap](https://github.com/microsoft/homebrew-apm/blob/421e62fae382774648ac7bf0103d98c686b117af/Formula/apm.rb)
and [core](https://github.com/Homebrew/homebrew-core/blob/99c61d24d4d3ade034b6542761977fed5cd1cc62/Formula/a/apm.rb)
formulas have no config-removal hooks. Production APM/Python bottles and
arbitrary older versions were not end-to-end tested.
:::

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

Pip installs do not edit shell profiles or write native shell setup policy. If your shell cannot find `apm`, add the Python user bin directory to `PATH` manually.

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

The automated path is `install.sh`; it selects the platform archive and checks the publisher `.sha256` before extracting:

```bash
curl -sSL https://aka.ms/apm-unix | sh
```

To avoid pipe-to-shell while retaining ownership checks and native shell setup, save and review the installer first:

```bash
curl -fsSL https://aka.ms/apm-unix -o install.sh
# Review ./install.sh before running it.
sh ./install.sh
```

For a manual archive install into an empty caller-owned prefix, choose an exact release tag that publishes both the archive and its `.sha256` sidecar. Set `ARCHIVE` to one value from the table, then run:

```bash
(
set -eu
TAG=v0.29.1          # replace with a release that publishes ARCHIVE and ARCHIVE.sha256
ARCHIVE=apm-linux-x86_64.tar.gz
BASE_URL=https://github.com/microsoft/apm/releases/download
INSTALL_ROOT="$HOME/.local"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/apm-manual.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

curl -fL --proto '=https' --tlsv1.2 -o "$ARCHIVE.sha256" "$BASE_URL/$TAG/$ARCHIVE.sha256"
if ! expected=$(LC_ALL=C awk -v asset="$ARCHIVE" '
  {
    sub(/\r$/, "")
    digest = substr($0, 1, 64)
    separator = substr($0, 65, 2)
    name = substr($0, 67)
    if (length(digest) == 64 && digest !~ /[^0-9a-fA-F]/ &&
        (separator == "  " || separator == " *") && name == asset) {
      matches++
      found = tolower(digest)
    } else {
      bad = 1
    }
  }
  END {
    if (bad || matches != 1 || NR != 1) exit 1
    print found
  }
' "$ARCHIVE.sha256"); then
  echo "Malformed checksum sidecar: choose a release with a valid $ARCHIVE.sha256." >&2
  exit 1
fi

curl -fL --proto '=https' --tlsv1.2 -o "$ARCHIVE" "$BASE_URL/$TAG/$ARCHIVE"
if command -v sha256sum >/dev/null 2>&1; then
  hash_line=$(sha256sum "$ARCHIVE")
elif command -v shasum >/dev/null 2>&1; then
  hash_line=$(shasum -a 256 "$ARCHIVE")
else
  echo "Install sha256sum or shasum before extracting." >&2
  exit 1
fi
actual=$(printf '%s\n' "$hash_line" | awk '{print tolower($1)}')
if [ "$actual" != "$expected" ]; then
  echo "Archive checksum verification failed." >&2
  exit 1
fi

tar -xzf "$ARCHIVE"
bundle=${ARCHIVE%.tar.gz}
"./$bundle/apm" --version

mkdir -p "$INSTALL_ROOT/lib/apm" "$INSTALL_ROOT/bin"
cp -R "$bundle"/. "$INSTALL_ROOT/lib/apm/"
ln -sf "$INSTALL_ROOT/lib/apm/apm" "$INSTALL_ROOT/bin/apm"
"$INSTALL_ROOT/bin/apm" --version
)
```

`tar` and the install steps run only after the SHA-256 comparison succeeds. This walkthrough checks same-publisher integrity, not signatures or independent provenance, and bypasses `install.sh` ownership, migration, and shell setup policy. Prefer the saved-installer path above unless you need to inspect every archive step manually.

If `$HOME/.local/bin` is not on `PATH`, add it using the activation syntax for your shell.

Use one of these archive names:

| Platform            | `ARCHIVE` value             |
|---------------------|-----------------------------|
| macOS Apple Silicon | `apm-darwin-arm64.tar.gz`   |
| macOS Intel         | `apm-darwin-x86_64.tar.gz`  |
| Linux x86_64        | `apm-linux-x86_64.tar.gz`   |
| Linux ARM64         | `apm-linux-arm64.tar.gz`    |

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

Use the `PATH` for your installation method:

- **Homebrew:** Follow the `brew shellenv` guidance from your Homebrew
  installation. If `brew` works, make sure `$(brew --prefix)/bin` is on `PATH`.
- **Standalone installer:** Open a new shell after a fresh native desktop
  install; bash, zsh, and fish are configured automatically when the install
  path and profile files are safe. The current terminal still needs manual
  activation because a child installer process cannot mutate its parent shell.
  If the installer skipped profile edits, run the exact shell-specific `PATH`
  command it printed: POSIX shells use `export`, and fish uses `set -gx`.
- **pip:** Activate the Python environment where you installed APM, or add its
  scripts directory to `PATH` manually. Pip fallback never edits profiles or
  writes native shell setup policy.

For native or pip script directories containing `:` or control characters, use
the printed absolute-path command instead.

If the wrong APM version runs, use `type -a apm` to find competing installations.
Put the intended installation first on `PATH`; update it with its owning package
manager, or use [self-update](../../reference/cli/self-update/) for standalone installs.

### Permission denied during install (macOS / Linux)

For existing installations, follow [ownership and migration](#unix-install-ownership-and-migration). Fresh destinations, including `--prefix`, must be writable, absolute paths without dot segments.

### Binary install fails on older Linux (devcontainers, Debian-based images)

Prebuilt Linux binaries (x86_64 and ARM64) require glibc 2.38+. Use a compatible base image (for example, `mcr.microsoft.com/devcontainers/universal:24-trixie`) or [pip with a working Python 3.10+](#pip-install). This glibc floor applies to prebuilt binaries, not your system Python. Eligible automatic fallback runs the selected interpreter's `python3 -m pip` or `python -m pip` command and follows the [ownership rules](#unix-install-ownership-and-migration). If that interpreter's user scripts directory cannot be represented as one `PATH` entry, use the absolute launcher command printed by the installer. Update or uninstall a fallback install with the same Python interpreter's `-m pip`; uninstall it before switching to the binary installer.

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