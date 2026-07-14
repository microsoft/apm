# APM CLI Installer Script (Windows / PowerShell)
#
# Usage:
#   irm https://aka.ms/apm-windows | iex
#
# Pin a version (skips GitHub HTTP API - use for air-gapped / GHE):
#   $env:VERSION = 'v1.2.3'; irm https://aka.ms/apm-windows | iex
#   .\install.ps1 v1.2.3
#
# Custom install location (contains apm.cmd; sibling current contains apm.exe):
#   $env:APM_INSTALL_DIR = "$env:LOCALAPPDATA\Programs\apm\bin"; irm ... | iex
#
# Fork or private mirror:
#   $env:APM_REPO = 'my-org/apm'; irm ... | iex
#
# GitHub Enterprise Server / mirror (set VERSION to avoid unreachable api.github.com):
#   $env:GITHUB_URL = 'https://github.corp.com'
#   $env:VERSION = 'v1.2.3'
#   irm https://.../install.ps1 | iex
#
# Enterprise bootstrap mirror:
#   $env:APM_RELEASE_BASE_URL = 'https://mirror.example/apm-releases'
#   $env:APM_RELEASE_METADATA_URL = 'https://mirror.example/apm-releases/latest.json'
#   $env:APM_INSTALLER_BASE_URL = 'https://mirror.example/apm-install'
#   $env:APM_PYPI_INDEX_URL = 'https://mirror.example/pypi/simple'
#   $env:APM_NO_DIRECT_FALLBACK = '1'
#
# Private repositories: set GITHUB_APM_PAT or GITHUB_TOKEN
#
# Pinned installs require a .sha256 sidecar unless you opt out:
#   $env:APM_SKIP_CHECKSUM = '1'   # or: .\install.ps1 v1.2.3 -SkipChecksum

param(
    [Parameter(Position = 0)]
    [string]$Version = $null,
    # Prefer $env:APM_REPO; -Repo remains for direct script invocation.
    [string]$Repo = "microsoft/apm",
    [switch]$SkipChecksum
)

$ErrorActionPreference = "Stop"

$skipChecksum = $SkipChecksum -or ($env:APM_SKIP_CHECKSUM -eq '1')

# ---------------------------------------------------------------------------
# Configuration (overridable via environment variables - parity with install.sh)
# ---------------------------------------------------------------------------

$githubUrl = if ($env:GITHUB_URL) {
    $env:GITHUB_URL.Trim().Trim('"').TrimEnd('/')
} else {
    "https://github.com"
}
if ($githubUrl -notmatch '(?i)^https://') {
    Write-Host "GITHUB_URL must use an https:// URL." -ForegroundColor Red
    exit 1
}

$apmRepo = if ($env:APM_REPO) { $env:APM_REPO.Trim() } else { $Repo }
if ($apmRepo -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    Write-Host "APM_REPO must be owner/name (letters, digits, ._- only)." -ForegroundColor Red
    exit 1
}

$releaseBaseUrl = if ($env:APM_RELEASE_BASE_URL) { $env:APM_RELEASE_BASE_URL.Trim().Trim('"').TrimEnd('/') } else { $null }
$releaseMetadataUrl = if ($env:APM_RELEASE_METADATA_URL) { $env:APM_RELEASE_METADATA_URL.Trim().Trim('"').TrimEnd('/') } else { $null }
$installerBaseUrl = if ($env:APM_INSTALLER_BASE_URL) { $env:APM_INSTALLER_BASE_URL.Trim().Trim('"').TrimEnd('/') } else { $null }
$pypiIndexUrl = if ($env:APM_PYPI_INDEX_URL) { $env:APM_PYPI_INDEX_URL.Trim().Trim('"').TrimEnd('/') } else { $null }
$noDirectFallback = $env:APM_NO_DIRECT_FALLBACK -match '^(?i:1|true|yes|on)$'

$pinnedVersion = $null
if ($env:VERSION) {
    $pinnedVersion = $env:VERSION.Trim().TrimStart('@')
} elseif ($Version) {
    $pinnedVersion = $Version.Trim().TrimStart('@')
}
if ($pinnedVersion -and $pinnedVersion -notmatch '^v?[0-9]+\.[0-9]+') {
    Write-Host "VERSION must look like a release tag (for example v1.2.3 or 1.2.3)." -ForegroundColor Red
    exit 1
}

$defaultInstallRoot = Join-Path $env:LOCALAPPDATA "Programs\apm"
$defaultBinDir = Join-Path $defaultInstallRoot "bin"

if ($env:APM_INSTALL_DIR) {
    $rawBinDir = $env:APM_INSTALL_DIR.Trim().TrimEnd('\', '/')
    $binDir = [System.IO.Path]::GetFullPath($rawBinDir)
    $parent = Split-Path $binDir -Parent
    if ($parent) {
        $installRoot = $parent
    } else {
        # Single-segment path: keep bundles next to the shim directory
        $installRoot = $binDir
    }
    $releasesDir = Join-Path $installRoot "releases"
} else {
    $installRoot = $defaultInstallRoot
    $binDir = $defaultBinDir
    $releasesDir = Join-Path $installRoot "releases"
}

$assetName = "apm-windows-x86_64.zip"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

function Get-GitHubApiRoot {
    param([string]$Url)
    $u = $Url.Trim().TrimEnd('/')
    if ($u -match '(?i)^https://github\.com$') {
        return "https://api.github.com"
    }
    return "$u/api/v3"
}

function Join-UrlPath {
    param(
        [string]$BaseUrl,
        [string[]]$Parts
    )
    $result = $BaseUrl.TrimEnd('/')
    foreach ($part in $Parts) {
        if ($part) {
            $result = "$result/$($part.Trim('/'))"
        }
    }
    return $result
}

function Redact-UrlCredentials {
    param([string]$Url)
    if (-not $Url) {
        return $Url
    }
    return ($Url -replace '([A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@', '$1***@')
}

function Get-ReleaseMetadataUri {
    if ($releaseMetadataUrl) {
        return $releaseMetadataUrl
    }
    return "$apiRoot/repos/$apmRepo/releases/latest"
}

function Get-ReleaseAssetUri {
    param(
        [string]$TagName,
        [string]$AssetName
    )
    if ($releaseBaseUrl) {
        return Join-UrlPath -BaseUrl $releaseBaseUrl -Parts @($TagName, $AssetName)
    }
    return "$githubUrl/$apmRepo/releases/download/$TagName/$AssetName"
}

function Get-PipIndexArgs {
    if ($pypiIndexUrl) {
        return @("--index-url", $pypiIndexUrl)
    }
    return @()
}

function Write-Info {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Green
}

function Write-WarningText {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Yellow
}

function Write-ErrorText {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Red
}

function Get-AuthHeader {
    # For GHES, use a PAT issued on that host (github.com tokens often will not work).
    if ($env:GITHUB_APM_PAT) {
        return @{ Authorization = "token $($env:GITHUB_APM_PAT)" }
    }
    if ($env:GITHUB_TOKEN) {
        return @{ Authorization = "token $($env:GITHUB_TOKEN)" }
    }
    return @{}
}

function Invoke-GitHubJson {
    param(
        [string]$Uri,
        [hashtable]$Headers
    )
    if ($Headers.Count -gt 0) {
        return Invoke-RestMethod -Uri $Uri -Headers $Headers
    }
    return Invoke-RestMethod -Uri $Uri
}

function Add-ToUserPath {
    param([string]$PathEntry)
    $currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $userEntries = @()
    if ($currentUserPath) {
        $userEntries = $currentUserPath.Split(";", [System.StringSplitOptions]::RemoveEmptyEntries)
    }
    if ($userEntries -notcontains $PathEntry) {
        $newUserPath = if ($currentUserPath) { "$PathEntry;$currentUserPath" } else { $PathEntry }
        [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
        Write-Info "Added $PathEntry to your user PATH."
    }
    if (($env:Path -split ";") -notcontains $PathEntry) {
        $env:Path = "$PathEntry;$env:Path"
    }
}

function Test-PythonRequirement {
    foreach ($cmd in @("python3", "python")) {
        $exe = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($exe) {
            try {
                $verStr = & $cmd -c "import sys; print('.'.join(map(str, sys.version_info[:2])))" 2>$null
                if ($verStr) {
                    $parts = $verStr.Split('.')
                    $major = [int]$parts[0]
                    $minor = [int]$parts[1]
                    if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 9)) {
                        return $cmd
                    }
                }
            } catch {
            }
        }
    }
    return $null
}

function Install-ViaPip {
    $pythonCmd = Test-PythonRequirement
    if (-not $pythonCmd) {
        Write-ErrorText "Python 3.9+ is not available - cannot fall back to pip."
        return $false
    }
    Write-Info "Attempting installation via pip ($pythonCmd)..."
    $pipCmd = $null
    foreach ($candidate in @("pip3", "pip")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            $pipCmd = $candidate
            break
        }
    }
    if (-not $pipCmd) {
        $pipCmd = "$pythonCmd -m pip"
    }
    if ($noDirectFallback -and -not $pypiIndexUrl) {
        Write-ErrorText "APM_NO_DIRECT_FALLBACK is set, but APM_PYPI_INDEX_URL is not configured."
        Write-Host "Set APM_PYPI_INDEX_URL to your internal PyPI proxy before using pip fallback."
        return $false
    }
    $pipPackageSpec = "apm-cli"
    if ($pinnedVersion) {
        $pipPackageSpec = "apm-cli==$($pinnedVersion.TrimStart('v'))"
    }
    $pipIndexArgs = Get-PipIndexArgs
    try {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            if ($pipCmd -like "* -m pip") {
                $output = & $pythonCmd -m pip install --user --upgrade @pipIndexArgs $pipPackageSpec 2>&1
                $pipExitCode = $LASTEXITCODE
                $output | Write-Host
            } else {
                $output = & $pipCmd install --user --upgrade @pipIndexArgs $pipPackageSpec 2>&1
                $pipExitCode = $LASTEXITCODE
                $output | Write-Host
            }
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($pipExitCode -ne 0) {
            Write-ErrorText "pip install failed (exit code $pipExitCode)."
            return $false
        }
    } catch {
        Write-ErrorText "pip install failed: $_"
        return $false
    }
    $apmExe = Get-Command apm -ErrorAction SilentlyContinue
    if ($apmExe) {
        $ver = & apm --version 2>$null
        Write-Success "APM installed successfully via pip! Version: $ver"
        Write-Info "Location: $($apmExe.Source)"
    } else {
        Write-WarningText "APM installed but not found in PATH."
        Write-Host "You may need to add your Python user scripts directory to PATH."
    }
    return $true
}

function Write-ManualInstallHelp {
    param(
        [string]$GithubUrl,
        [string]$ApmRepo
    )
    Write-Host ""
    Write-Info "Manual installation options:"
    if ($pypiIndexUrl) {
        Write-Host "  1. pip (recommended): pip install --user --index-url $pypiIndexUrl apm-cli"
    } elseif ($noDirectFallback) {
        Write-Host "  1. pip (recommended): set APM_PYPI_INDEX_URL, then run pip install --user --index-url <mirror> apm-cli"
    } else {
        Write-Host "  1. pip (recommended): pip install --user apm-cli"
    }
    Write-Host "  2. From source:"
    Write-Host "     git clone $GithubUrl/${ApmRepo}.git"
    Write-Host "     cd apm && uv sync && uv run pip install -e ."
    Write-Host ""
    Write-Host "Need help? Create an issue at: $GithubUrl/$ApmRepo/issues"
}

function Get-Sha256Hex {
    # Stream-based SHA256 that works even when Get-FileHash is unavailable
    # (hardened hosts, $PSModuleAutoLoadingPreference='None', restricted sessions).
    # System.Security.Cryptography is a core .NET type allowed in ConstrainedLanguage.
    param([string]$Path)
    $cmd = Get-Command Get-FileHash -ErrorAction SilentlyContinue
    if (-not $cmd) {
        try {
            Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
            $cmd = Get-Command Get-FileHash -ErrorAction SilentlyContinue
        } catch {
        }
    }
    if ($cmd) {
        return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLower()
    }
    $stream = $null
    $hasher = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $hasher = [System.Security.Cryptography.SHA256]::Create()
        $bytes = $hasher.ComputeHash($stream)
        $sb = New-Object System.Text.StringBuilder
        foreach ($b in $bytes) { [void]$sb.Append($b.ToString("x2")) }
        return $sb.ToString()
    } finally {
        if ($hasher) { $hasher.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}

function Test-AccessDeniedError {
    # AppLocker / WDAC / App Control for Business / SRP / Group Policy deny
    # CreateProcess on EXEs under user-writable paths (e.g. %TEMP%,
    # %LOCALAPPDATA%\Temp) with one of:
    #   0x80070005 (E_ACCESSDENIED, Win32 5)         -> "Access is denied"
    #   0x800704EC (ERROR_ACCESS_DISABLED_BY_POLICY, -> "This program is
    #               Win32 1260)                          blocked by group policy"
    # Both belong in the AppControl/AppLocker guidance bucket.
    param([string]$Text)
    if (-not $Text) { return $false }
    return (
        $Text -match 'Access is denied' -or
        $Text -match 'blocked by group policy' -or
        $Text -match '0x80070005' -or
        $Text -match '0x800704EC'
    )
}

function Test-AntivirusBlockError {
    # Defender / 3rd-party AV real-time protection blocks CreateProcess on
    # binaries it flags with HRESULT 0x800700E1 (ERROR_VIRUS_INFECTED,
    # Win32 225) or 0x800700E2 (ERROR_VIRUS_DELETED, Win32 226). PowerShell
    # surfaces these as "Operation did not complete successfully because
    # the file contains a virus or potentially unwanted software". Our
    # PyInstaller-built apm.exe is unsigned, which routinely trips
    # false-positive heuristics.
    param([string]$Text)
    if (-not $Text) { return $false }
    return (
        $Text -match 'contains a virus' -or
        $Text -match 'potentially unwanted software' -or
        $Text -match '0x800700E1' -or
        $Text -match '0x800700E2'
    )
}

function Write-AppControlGuidance {
    param(
        [string]$Path,
        [string]$TargetInstallDir
    )
    Write-Host ""
    Write-ErrorText "The OS denied execution of $Path."
    Write-Host "This is the standard signature of an enterprise application control policy"
    Write-Host "(AppLocker or App Control for Business / WDAC) denying an unsigned binary"
    Write-Host "from a user-writable path."
    Write-Host ""
    Write-Info "Options to unblock:"
    if ($TargetInstallDir) {
        Write-Host "  1. Ask your endpoint admin to allow-list the final install path"
        Write-Host "     ($TargetInstallDir) via an AppLocker/WDAC Path or Publisher rule."
    } else {
        Write-Host "  1. Ask your endpoint admin to allow-list apm.exe via an"
        Write-Host "     AppLocker/WDAC Path or Publisher rule."
    }
    Write-Host "  2. Set APM_TEMP_DIR to a directory your policy permits, then retry:"
    Write-Host "       `$env:APM_TEMP_DIR = `"`$env:LOCALAPPDATA\Programs\apm\tmp`""
    Write-Host "  3. Install via pip into your user site:"
    Write-Host "       pip install --user apm-cli"
    Write-Host ""
}

function Write-AntivirusGuidance {
    param(
        [string]$Path,
        [string]$TargetInstallDir
    )
    Write-Host ""
    Write-ErrorText "An antivirus product blocked execution of $Path."
    Write-Host "Windows Defender (or another real-time scanner) flagged the binary."
    Write-Host "The apm.exe release is built with PyInstaller and is currently"
    Write-Host "unsigned, which routinely trips false-positive heuristics on"
    Write-Host "unsigned binaries. Most blocks of apm.exe are false positives, but"
    Write-Host "you should verify integrity before excluding it:"
    Write-Host ""
    Write-Host "  1. Verify the SHA256 of apm.exe against the published .sha256"
    Write-Host "     sidecar on the release page before adding any AV exclusion."
    Write-Host "     Do not exclude a binary whose checksum you have not verified."
    Write-Host ""
    Write-Info "If the checksum matches, options to unblock:"
    if ($TargetInstallDir) {
        # Single-quote-escape the path so the printed command stays valid
        # even if the install directory contains a "'" character (rare but
        # possible in usernames).
        $escapedDir = $TargetInstallDir -replace "'", "''"
        Write-Host "  a. Add a Defender exclusion for the install directory (run in"
        Write-Host "     an elevated PowerShell, then rerun this installer):"
        Write-Host "       Add-MpPreference -ExclusionPath '$escapedDir'"
    } else {
        Write-Host "  a. Add a Defender exclusion for apm.exe (run in an elevated"
        Write-Host "     PowerShell, then rerun this installer):"
        Write-Host "       Add-MpPreference -ExclusionProcess 'apm.exe'"
    }
    Write-Host "  b. Install via pip into your user site (avoids the binary entirely):"
    Write-Host "       pip install --user apm-cli"
    Write-Host "  c. Help us get the false positive cleared by submitting the binary"
    Write-Host "     to Microsoft: https://www.microsoft.com/en-us/wdsi/filesubmission"
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "===========================================================" -ForegroundColor Blue
Write-Host "                    APM Installer                          " -ForegroundColor Blue
Write-Host "             The NPM for AI-Native Development             " -ForegroundColor Blue
Write-Host "===========================================================" -ForegroundColor Blue
Write-Host ""

$apiRoot = Get-GitHubApiRoot -Url $githubUrl
$headers = @{}

# ---------------------------------------------------------------------------
# Stage 1 - Release metadata (skip GitHub API when VERSION is pinned)
# ---------------------------------------------------------------------------

$release = $null
$asset = $null
$tagName = $null

if ($pinnedVersion) {
    $tagName = $pinnedVersion
    Write-Success "Version: $tagName (pinned - skipping releases/latest API)"
} else {
    Write-Info "Fetching latest release information..."
    if ($noDirectFallback -and -not $releaseMetadataUrl -and $githubUrl -match '(?i)^https://github\.com$') {
        Write-ErrorText "APM_NO_DIRECT_FALLBACK is set, but APM_RELEASE_METADATA_URL is not configured."
        Write-Host "Set APM_RELEASE_METADATA_URL to mirrored latest.json, or set VERSION to a pinned release."
        exit 1
    }
    $latestUri = Get-ReleaseMetadataUri
    # Mirror metadata URLs must never receive GitHub/GHES credentials.
    $headers = if ($releaseMetadataUrl) { @{} } else { Get-AuthHeader }
    $metadataError = $null
    try {
        $release = Invoke-GitHubJson -Uri $latestUri -Headers $headers
    } catch {
        $metadataError = $_
    }

    if ($releaseMetadataUrl -and (-not $release -or -not $release.tag_name)) {
        Write-ErrorText "Failed to fetch release metadata from APM_RELEASE_METADATA_URL."
        Write-Host "Mirror URL: $(Redact-UrlCredentials -Url $releaseMetadataUrl)"
        if ($metadataError) { Write-Host "Details: $(Redact-UrlCredentials -Url $metadataError)" }
        Write-Host "Publish a GitHub-compatible latest.json document with a tag_name field."
        exit 1
    }

    if (-not $release -or -not $release.tag_name) {
        Write-Info "Unauthenticated request failed or returned no data. Retrying with authentication..."
        $headers = Get-AuthHeader
        if ($headers.Count -eq 0) {
            Write-ErrorText "Repository may be private but no authentication token found."
            Write-Host "Set GITHUB_APM_PAT or GITHUB_TOKEN and retry."
            Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
            exit 1
        }
        try {
            $release = Invoke-GitHubJson -Uri $latestUri -Headers $headers
        } catch {
            Write-ErrorText "Failed to fetch release information: $_"
            Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
            exit 1
        }
    }

    if (-not $release.tag_name) {
        Write-ErrorText "Could not determine the latest release tag."
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }

    $tagName = $release.tag_name
    if (-not $releaseBaseUrl) {
        $asset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
        if (-not $asset) {
            Write-ErrorText "Release $tagName does not contain $assetName."
            Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
            exit 1
        }
    }
    Write-Success "Latest version: $tagName"
}

$releaseDir = Join-Path $releasesDir $tagName
$tempRootInput = if ($env:APM_TEMP_DIR) {
    $env:APM_TEMP_DIR.Trim().Trim('"').TrimEnd('\', '/')
} else {
    [System.IO.Path]::GetTempPath()
}

$tempRootDir = $null
try {
    $tempRootDir = [System.IO.Path]::GetFullPath($tempRootInput)
    New-Item -ItemType Directory -Force -Path $tempRootDir | Out-Null
} catch {
    Write-ErrorText "Failed to prepare temporary staging root: $_"
    if ($tempRootDir) {
        Write-Host "Temporary staging root was: $tempRootDir"
    } else {
        Write-Host "Temporary staging root was: $tempRootInput"
    }
    Write-Host "Set APM_TEMP_DIR to a writable directory allowed by endpoint policy, then retry:"
    Write-Host "  `$env:APM_TEMP_DIR = `"`$env:LOCALAPPDATA\Programs\apm\tmp`""
    Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
    exit 1
}

$tempDir = Join-Path $tempRootDir ("apm-install-" + [System.Guid]::NewGuid().ToString("N"))
$zipPath = Join-Path $tempDir $assetName

try {
    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    New-Item -ItemType Directory -Force -Path $releasesDir | Out-Null
} catch {
    Write-ErrorText "Failed to prepare installer directories: $_"
    Write-Host "Temporary staging directory: $tempDir"
    Write-Host "Install bin directory: $binDir"
    Write-Host "Releases directory: $releasesDir"
    Write-Host "If temporary staging was denied, set APM_TEMP_DIR to a writable directory allowed by endpoint policy, then retry:"
    Write-Host "  `$env:APM_TEMP_DIR = `"`$env:LOCALAPPDATA\Programs\apm\tmp`""
    Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
    exit 1
}

try {
    # ------------------------------------------------------------------
    # Stage 2 - Download binary
    # ------------------------------------------------------------------

    Write-Info "Downloading $assetName ($tagName)..."

    $downloadOk = $false
    if ($noDirectFallback -and -not $releaseBaseUrl -and $githubUrl -match '(?i)^https://github\.com$') {
        Write-ErrorText "APM_NO_DIRECT_FALLBACK is set, but APM_RELEASE_BASE_URL is not configured."
        Write-Host "Set APM_RELEASE_BASE_URL to a mirror containing $tagName/$assetName."
        exit 1
    }
    $directUrl = Get-ReleaseAssetUri -TagName $tagName -AssetName $assetName

    if ($pinnedVersion) {
        $pinDownloadErr = $null
        try {
            Invoke-WebRequest -Uri $directUrl -OutFile $zipPath -UseBasicParsing
            $downloadOk = $true
            Write-Success "Download successful"
        } catch {
            $pinDownloadErr = $_.Exception.Message
            Write-WarningText "Unauthenticated download failed, retrying with authentication..."
        }
        if (-not $downloadOk) {
            if ($headers.Count -eq 0) { $headers = Get-AuthHeader }
            if ($headers.Count -eq 0) {
                Write-ErrorText "Repository may be private but no authentication token found."
                Write-Host "Set GITHUB_APM_PAT or GITHUB_TOKEN and retry."
                if ($pinDownloadErr) {
                    Write-Host "Details: $pinDownloadErr"
                }
                Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
                exit 1
            }
            try {
                Invoke-WebRequest -Uri $directUrl -Headers $headers -OutFile $zipPath -UseBasicParsing
                $downloadOk = $true
                Write-Success "Download successful with authentication"
            } catch {
                Write-WarningText "Authenticated download failed: $($_.Exception.Message)"
            }
        }
    } else {
        try {
            $initialAssetUri = if ($releaseBaseUrl) { $directUrl } else { $asset.browser_download_url }
            Invoke-WebRequest -Uri $initialAssetUri -OutFile $zipPath -UseBasicParsing
            $downloadOk = $true
            Write-Success "Download successful"
        } catch {
            Write-WarningText "Unauthenticated download failed, retrying with authentication..."
        }

        if (-not $downloadOk -and -not $releaseBaseUrl) {
            if ($headers.Count -eq 0) { $headers = Get-AuthHeader }
            if ($headers.Count -gt 0 -and $asset.url) {
                try {
                    $apiHeaders = @{} + $headers
                    $apiHeaders["Accept"] = "application/octet-stream"
                    Invoke-WebRequest -Uri $asset.url -Headers $apiHeaders -OutFile $zipPath -UseBasicParsing
                    $downloadOk = $true
                    Write-Success "Download successful via GitHub API"
                } catch {
                    Write-WarningText "API download failed, trying direct URL with auth..."
                }
            }
        }

        # Final auth fallback only for canonical GitHub / GHES hosts. In mirror mode
        # ($releaseBaseUrl set) the GitHub token must never be sent to the operator
        # mirror host, so skip auth and fail closed via the mirror error below.
        if (-not $downloadOk -and -not $releaseBaseUrl) {
            if ($headers.Count -eq 0) { $headers = Get-AuthHeader }
            if ($headers.Count -gt 0) {
                try {
                    Invoke-WebRequest -Uri $asset.browser_download_url -Headers $headers -OutFile $zipPath -UseBasicParsing
                    $downloadOk = $true
                    Write-Success "Download successful with authentication"
                } catch {
                }
            }
        }
    }

    if (-not $downloadOk -and $releaseBaseUrl) {
        Write-ErrorText "Failed to download APM CLI from APM_RELEASE_BASE_URL mirror."
        Write-Host "Mirror URL was: $(Redact-UrlCredentials -Url $directUrl)"
        Write-Host "Check that the mirror is reachable and contains $tagName/$assetName."
        exit 1
    }

    if (-not $downloadOk) {
        Write-ErrorText "All download attempts failed."
        Write-Host "Direct URL was: $(Redact-UrlCredentials -Url $directUrl)"
        Write-Host "This might mean:"
        Write-Host "  - Network connectivity issues"
        Write-Host "  - Invalid GitHub token or insufficient permissions"
        Write-Host "  - Private repository requires authentication"
        Write-Host ""

        Write-Info "Attempting automatic fallback to pip..."
        if (Install-ViaPip) { exit 0 }
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }

    # ------------------------------------------------------------------
    # Verify checksum (pinned installs require .sha256 unless skipped)
    # ------------------------------------------------------------------

    $sha256AssetName = "$assetName.sha256"
    $sha256Url = Get-ReleaseAssetUri -TagName $tagName -AssetName $sha256AssetName

    $sha256Source = $null
    if (-not $pinnedVersion -and -not $releaseBaseUrl) {
        $shaObj = $release.assets | Where-Object { $_.name -eq $sha256AssetName } | Select-Object -First 1
        if ($shaObj) { $sha256Source = $shaObj }
    }

    $checksumRequired = [bool]($pinnedVersion -and -not $skipChecksum)

    if ($skipChecksum -and $pinnedVersion) {
        Write-WarningText "Skipping checksum verification (APM_SKIP_CHECKSUM or -SkipChecksum)."
    } elseif ($sha256Source -or $pinnedVersion) {
        Write-Info "Verifying download checksum..."
        $sha256Path = Join-Path $tempDir $sha256AssetName
        $fetched = $false
        try {
            if ($sha256Source) {
                try {
                    Invoke-WebRequest -Uri $sha256Source.browser_download_url -OutFile $sha256Path -UseBasicParsing
                    $fetched = $true
                } catch {
                    Write-WarningText "Unauthenticated checksum download failed, retrying with authentication..."
                    if ($headers.Count -eq 0) { $headers = Get-AuthHeader }
                    if ($headers.Count -eq 0) { throw }
                    try {
                        Invoke-WebRequest -Uri $sha256Source.browser_download_url -Headers $headers -OutFile $sha256Path -UseBasicParsing
                        $fetched = $true
                    } catch {
                        if (-not $sha256Source.url) { throw }
                        $apiHeaders = @{} + $headers
                        $apiHeaders["Accept"] = "application/octet-stream"
                        Invoke-WebRequest -Uri $sha256Source.url -Headers $apiHeaders -OutFile $sha256Path -UseBasicParsing
                        $fetched = $true
                    }
                }
            } else {
                try {
                    Invoke-WebRequest -Uri $sha256Url -OutFile $sha256Path -UseBasicParsing
                    $fetched = $true
                } catch {
                    # Mirror checksum URLs ($releaseBaseUrl set) stay unauthenticated:
                    # never send the GitHub token to the operator mirror host.
                    if ($headers.Count -eq 0) { $headers = Get-AuthHeader }
                    if ($headers.Count -gt 0 -and -not $releaseBaseUrl) {
                        Invoke-WebRequest -Uri $sha256Url -Headers $headers -OutFile $sha256Path -UseBasicParsing
                        $fetched = $true
                    } else {
                        throw
                    }
                }
            }
        } catch {
            if ($checksumRequired) {
                Write-ErrorText "Could not download checksum file for pinned install."
                Write-Host "$_"
                Write-Host "Expected: $sha256Url"
                Write-Host "To bypass integrity verification (emergency only), set APM_SKIP_CHECKSUM=1 or pass -SkipChecksum."
                Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
                exit 1
            }
            Write-WarningText "Could not download checksum file (non-fatal): $_"
        }

        if ($checksumRequired -and -not $fetched) {
            Write-ErrorText "Pinned install requires the release .sha256 file next to the zip."
            Write-Host "Expected: $sha256Url"
            Write-Host "To bypass integrity verification (emergency only), set APM_SKIP_CHECKSUM=1 or pass -SkipChecksum."
            Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
            exit 1
        }

        if ($fetched -and (Test-Path $sha256Path)) {
            try {
                $expectedHash = (Get-Content $sha256Path -Raw).Trim().Split(" ")[0]
                $actualHash = Get-Sha256Hex -Path $zipPath
                if ($actualHash -ne $expectedHash) {
                    Write-ErrorText "Checksum verification FAILED."
                    Write-Host "  Expected: $expectedHash"
                    Write-Host "  Actual:   $actualHash"
                    Write-Info "Attempting automatic fallback to pip..."
                    if (Install-ViaPip) { exit 0 }
                    Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
                    exit 1
                }
                Write-Success "Checksum verified"
            } catch {
                if ($checksumRequired) {
                    Write-ErrorText "Checksum verification failed: $_"
                    Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
                    exit 1
                }
                Write-WarningText "Could not verify checksum (non-fatal): $_"
            }
        } elseif ($checksumRequired) {
            Write-ErrorText "Checksum file missing after download."
            Write-Host "Expected: $sha256Url"
            Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
            exit 1
        }
    }

    # ------------------------------------------------------------------
    # Extract + stage + binary test + promote
    #
    # Order matters: AppLocker / App Control for Business commonly block
    # executable launch from %TEMP%. We move the extracted bundle to the
    # final per-user install root ($releasesDir, default
    # %LOCALAPPDATA%\Programs\apm\releases\<tag>) BEFORE invoking
    # apm.exe --version, so the binary test runs from the allow-listed
    # path that the shim will keep pointing at. Until promotion succeeds
    # we stage to a sibling `.new-<guid>` directory so a failed install
    # never destroys the currently working release. See issue #1389.
    # ------------------------------------------------------------------

    Write-Info "Extracting package..."
    Expand-Archive -Path $zipPath -DestinationPath $tempDir -Force

    $packageDir = Join-Path $tempDir "apm-windows-x86_64"
    if (-not (Test-Path $packageDir)) {
        Write-ErrorText "Extracted package is missing the apm-windows-x86_64 directory."
        Write-Info "Attempting automatic fallback to pip..."
        if (Install-ViaPip) { exit 0 }
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }

    $stagingDir = "$releaseDir.new-" + [System.Guid]::NewGuid().ToString("N")
    if (Test-Path $stagingDir) {
        Remove-Item -Recurse -Force $stagingDir
    }
    try {
        Move-Item -Path $packageDir -Destination $stagingDir -Force
    } catch {
        $stageError = "$_"
        Write-ErrorText "Failed to stage release at ${stagingDir}: $stageError"
        if (Test-AccessDeniedError -Text $stageError) {
            Write-AppControlGuidance -Path $stagingDir -TargetInstallDir $releaseDir
        }
        Write-Info "Attempting automatic fallback to pip..."
        if (Install-ViaPip) { exit 0 }
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }

    $stagedExe = Join-Path $stagingDir "apm.exe"
    if (-not (Test-Path $stagedExe)) {
        Write-ErrorText "Staged package is missing apm.exe."
        Remove-Item -Recurse -Force $stagingDir -ErrorAction SilentlyContinue
        Write-Info "Attempting automatic fallback to pip..."
        if (Install-ViaPip) { exit 0 }
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }

    Write-Info "Testing binary..."
    $testFailure = $null
    try {
        $testOutput = & $stagedExe --version 2>&1
        if ($LASTEXITCODE -ne 0) { throw "exit code $LASTEXITCODE - $testOutput" }
        Write-Success "Binary test successful: $testOutput"
    } catch {
        $testFailure = "$_"
    }

    if ($testFailure) {
        $denied = Test-AccessDeniedError -Text $testFailure
        $avBlocked = Test-AntivirusBlockError -Text $testFailure
        Write-ErrorText "Downloaded binary failed to run: $testFailure"
        if ($avBlocked) {
            Write-AntivirusGuidance -Path $stagedExe -TargetInstallDir $releaseDir
        } elseif ($denied) {
            Write-AppControlGuidance -Path $stagedExe -TargetInstallDir $releaseDir
        }
        Remove-Item -Recurse -Force $stagingDir -ErrorAction SilentlyContinue
        Write-Info "Attempting automatic fallback to pip..."
        if (Install-ViaPip) { exit 0 }
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }

    # Promote: rename the existing release aside, then rename the staged
    # tree into place. Win32 has no truly atomic directory replacement, so
    # there is still a small gap where neither path exists; doing it this
    # way minimizes that gap and lets us roll back if the second rename
    # fails. Concurrent apm invocations during that window will fail and
    # need a retry -- acceptable for an install/self-update operation.
    $backupDir = $null
    if (Test-Path $releaseDir) {
        $backupDir = "$releaseDir.old-" + [System.Guid]::NewGuid().ToString("N")
        try {
            Move-Item -Path $releaseDir -Destination $backupDir -Force
        } catch {
            Write-ErrorText "Failed to move existing release aside: $_"
            Remove-Item -Recurse -Force $stagingDir -ErrorAction SilentlyContinue
            Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
            exit 1
        }
    }

    try {
        Move-Item -Path $stagingDir -Destination $releaseDir -Force
    } catch {
        Write-ErrorText "Failed to promote staged release: $_"
        if ($backupDir -and (Test-Path $backupDir)) {
            Move-Item -Path $backupDir -Destination $releaseDir -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -Recurse -Force $stagingDir -ErrorAction SilentlyContinue
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }

    if ($backupDir -and (Test-Path $backupDir)) {
        Remove-Item -Recurse -Force $backupDir -ErrorAction SilentlyContinue
    }

    # Expose the complete onedir bundle through a version-stable junction.
    # Putting this directory on PATH lets CreateProcess callers resolve the
    # real apm.exe while keeping its sibling PyInstaller runtime files intact.
    $currentDir = Join-Path $installRoot "current"
    $currentExe = Join-Path $currentDir "apm.exe"
    $newCurrentDir = "$currentDir.new-" + [System.Guid]::NewGuid().ToString("N")
    $oldCurrentDir = $null
    try {
        New-Item -ItemType Junction -Path $newCurrentDir -Target $releaseDir | Out-Null

        if (Test-Path $currentDir) {
            $currentItem = Get-Item -Force $currentDir
            if (($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) {
                throw "Refusing to replace non-junction path. Move or remove it if safe, then rerun the installer."
            }
            $oldCurrentDir = "$currentDir.old-" + [System.Guid]::NewGuid().ToString("N")
            Move-Item -Path $currentDir -Destination $oldCurrentDir -Force
        }

        Move-Item -Path $newCurrentDir -Destination $currentDir -Force
    } catch {
        Write-ErrorText "Failed to update stable executable path ${currentDir}: $_"
        if (Test-Path $newCurrentDir) {
            try { [System.IO.Directory]::Delete($newCurrentDir) } catch { Write-ErrorText "Could not remove temp junction ${newCurrentDir}: $_" }
        }
        if ($oldCurrentDir -and (Test-Path $oldCurrentDir) -and -not (Test-Path $currentDir)) {
            Move-Item -Path $oldCurrentDir -Destination $currentDir -Force -ErrorAction SilentlyContinue
        }
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }
    if ($oldCurrentDir -and (Test-Path $oldCurrentDir)) {
        # Directory.Delete removes only the junction. Windows PowerShell 5.1
        # Remove-Item prompts for its non-empty target in NonInteractive mode.
        [System.IO.Directory]::Delete($oldCurrentDir)
    }
    if (-not (Test-Path $currentExe)) {
        Write-ErrorText "Stable executable path is missing apm.exe: $currentExe"
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }

    $shimPath = Join-Path $binDir "apm.cmd"
    # Prefer the literal %LOCALAPPDATA% token over the expanded profile path
    # so cmd.exe resolves the shim target at runtime. This avoids "The
    # system cannot find the path specified." on accounts whose profile
    # directory contains non-ASCII characters (issue microsoft/apm#1509).
    $localAppData = $env:LOCALAPPDATA
    $localAppDataTrimmed = if ($localAppData) { $localAppData.TrimEnd('\', '/') } else { $null }
    # Enforce a path-separator boundary so sibling directories that merely
    # share a textual prefix (e.g. "C:\Users\x\AppData\LocalStuff\...") are
    # not rewritten under %LOCALAPPDATA%.
    $underLocalAppData = $false
    if ($localAppDataTrimmed) {
        $prefixWithSep = $localAppDataTrimmed + '\'
        if ($releaseDir.Equals($localAppDataTrimmed, [System.StringComparison]::OrdinalIgnoreCase) -or
            $releaseDir.StartsWith($prefixWithSep, [System.StringComparison]::OrdinalIgnoreCase)) {
            $underLocalAppData = $true
        }
    }
    if ($underLocalAppData) {
        $relative = $releaseDir.Substring($localAppDataTrimmed.Length).TrimStart('\', '/')
        # Escape any literal '%' in the relative segment so cmd.exe does
        # not attempt to expand accidental env-var references (e.g. a
        # custom APM_INSTALL_DIR under %LOCALAPPDATA% that contains a
        # literal percent sign). The leading %LOCALAPPDATA% token MUST
        # stay unescaped so cmd.exe expands it at runtime.
        $relativeEscaped = $relative -replace '%', '%%'
        $shimTarget = "%LOCALAPPDATA%\$relativeEscaped\apm.exe"
    } else {
        # Escape any literal '%' in the absolute release directory for
        # the same reason; without escaping, cmd.exe would treat
        # "%foo%" in a custom APM_INSTALL_DIR as an env-var reference.
        $releaseDirEscaped = $releaseDir -replace '%', '%%'
        $shimTarget = "$releaseDirEscaped\apm.exe"
    }
    # Embed two short advisory REM lines so anyone who opens apm.cmd in
    # an editor understands the file is generated and that cmd.exe
    # expands the %LOCALAPPDATA% token at runtime; hand-edits that
    # hard-code the expanded profile path re-introduce the bug from
    # issue #1509. Two short lines wrap better than one long one.
    $shimContent = "@echo off`r`nREM Generated by install.ps1 (microsoft/apm#1509) -- do not hand-edit.`r`nREM cmd.exe expands %LOCALAPPDATA% at runtime; hand-edited paths break.`r`n`"$shimTarget`" %*`r`n"
    # Write the shim as ASCII. cmd.exe interprets .cmd files via the system
    # OEM/ANSI code page and does NOT reliably auto-detect UTF-16LE (even
    # with a BOM) when batch files are invoked via PATH or double-click; a
    # UTF-16 shim surfaces as garbled bytes (the cmd.exe prompt followed
    # by replacement-character noise) and exit code 1.
    # ASCII is safe for our payload because the %LOCALAPPDATA% literal
    # token (issue #1509) keeps the embedded shim target ASCII-only even
    # when the user's profile directory contains non-ASCII characters.
    Set-Content -Path $shimPath -Value $shimContent -Encoding ASCII -NoNewline

    Add-ToUserPath -PathEntry $binDir
    # The onedir bundle must stay intact beside apm.exe. Add its stable
    # junction so bare executable lookup finds apm.exe where PATHEXT is absent
    # (CreateProcess, Git Bash). Add-ToUserPath prepends, so this entry
    # precedes bin in PATH.
    Add-ToUserPath -PathEntry $currentDir

    Write-Host ""
    Write-Success "APM $tagName installed successfully!"
    Write-Info "Command shim: $shimPath"
    Write-Info "Stable executable: $currentExe"
    Write-Host ""
    Write-Info "Quick start:"
    Write-Host "  apm init my-app          # Create a new APM project"
    Write-Host "  cd my-app && apm install # Install dependencies"
    Write-Host "  apm run                  # Run your first prompt"
    Write-Host ""
    Write-Host "Documentation: $githubUrl/$apmRepo"
    Write-Info "Run 'apm --version' in a new terminal to verify the installation."
} finally {
    if (Test-Path $tempDir) {
        Remove-Item -Recurse -Force $tempDir
    }
}
