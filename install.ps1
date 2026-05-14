# APM CLI Installer Script (Windows / PowerShell)
#
# Usage:
#   irm https://aka.ms/apm-windows | iex
#
# Pin a version (skips GitHub HTTP API - use for air-gapped / GHE):
#   $env:VERSION = 'v1.2.3'; irm https://aka.ms/apm-windows | iex
#   .\install.ps1 v1.2.3
#
# Custom install location (directory that will contain apm.cmd):
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
    try {
        if ($pipCmd -like "* -m pip") {
            $output = & $pythonCmd -m pip install --user apm-cli 2>&1
            $pipExitCode = $LASTEXITCODE
            $output | Write-Host
        } else {
            $output = & $pipCmd install --user apm-cli 2>&1
            $pipExitCode = $LASTEXITCODE
            $output | Write-Host
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
    Write-Host "  1. pip (recommended): pip install --user apm-cli"
    Write-Host "  2. From source:"
    Write-Host "     git clone $GithubUrl/${ApmRepo}.git"
    Write-Host "     cd apm && uv sync && uv run pip install -e ."
    Write-Host ""
    Write-Host "Need help? Create an issue at: $GithubUrl/$ApmRepo/issues"
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
    $latestUri = "$apiRoot/repos/$apmRepo/releases/latest"
    try {
        $release = Invoke-RestMethod -Uri $latestUri
    } catch {
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
    $asset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
    if (-not $asset) {
        Write-ErrorText "Release $tagName does not contain $assetName."
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }
    Write-Success "Latest version: $tagName"
}

$releaseDir = Join-Path $releasesDir $tagName
$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("apm-install-" + [System.Guid]::NewGuid().ToString("N"))
$zipPath = Join-Path $tempDir $assetName

New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
New-Item -ItemType Directory -Force -Path $releasesDir | Out-Null

try {
    # ------------------------------------------------------------------
    # Stage 2 - Download binary
    # ------------------------------------------------------------------

    Write-Info "Downloading $assetName ($tagName)..."

    $downloadOk = $false
    $directUrl = "$githubUrl/$apmRepo/releases/download/$tagName/$assetName"

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
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing
            $downloadOk = $true
            Write-Success "Download successful"
        } catch {
            Write-WarningText "Unauthenticated download failed, retrying with authentication..."
        }

        if (-not $downloadOk) {
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

        if (-not $downloadOk) {
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

    if (-not $downloadOk) {
        Write-ErrorText "All download attempts failed."
        Write-Host "Direct URL was: $directUrl"
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
    $sha256Url = "$githubUrl/$apmRepo/releases/download/$tagName/$sha256AssetName"

    $sha256Source = $null
    if (-not $pinnedVersion) {
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
                    if ($headers.Count -eq 0) { $headers = Get-AuthHeader }
                    if ($headers.Count -gt 0) {
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
                $actualHash = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLower()
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
    # Extract
    # ------------------------------------------------------------------

    Write-Info "Extracting package..."
    Expand-Archive -Path $zipPath -DestinationPath $tempDir -Force

    $packageDir = Join-Path $tempDir "apm-windows-x86_64"
    $exePath = Join-Path $packageDir "apm.exe"
    if (-not (Test-Path $exePath)) {
        Write-ErrorText "Extracted package is missing apm.exe."
        Write-Info "Attempting automatic fallback to pip..."
        if (Install-ViaPip) { exit 0 }
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }

    # ------------------------------------------------------------------
    # Binary test
    # ------------------------------------------------------------------

    Write-Info "Testing binary..."
    try {
        $testOutput = & $exePath --version 2>&1
        if ($LASTEXITCODE -ne 0) { throw "exit code $LASTEXITCODE" }
        Write-Success "Binary test successful: $testOutput"
    } catch {
        Write-ErrorText "Downloaded binary failed to run: $_"
        Write-Host ""
        Write-Info "Attempting automatic fallback to pip..."
        if (Install-ViaPip) { exit 0 }
        Write-ManualInstallHelp -GithubUrl $githubUrl -ApmRepo $apmRepo
        exit 1
    }

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    if (Test-Path $releaseDir) {
        Remove-Item -Recurse -Force $releaseDir
    }

    Move-Item -Path $packageDir -Destination $releaseDir

    $shimPath = Join-Path $binDir "apm.cmd"
    $shimContent = "@echo off`r`n`"$releaseDir\apm.exe`" %*`r`n"
    Set-Content -Path $shimPath -Value $shimContent -Encoding ASCII

    Add-ToUserPath -PathEntry $binDir

    Write-Host ""
    Write-Success "APM $tagName installed successfully!"
    Write-Info "Command shim: $shimPath"
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
