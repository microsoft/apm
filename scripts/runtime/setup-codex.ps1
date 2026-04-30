# Setup script for Codex runtime (Windows)
# Downloads Codex binary from GitHub releases and configures with GitHub Models

# Pin to a known stable release for security and reproducibility (#662).
# Users can override with: apm runtime setup codex --version <version> (e.g. 'latest')
param(
    [switch]$Vanilla,
    [string]$Version = "rust-v0.118.0"
)

$ErrorActionPreference = "Stop"

# Source common utilities
. "$PSScriptRoot\setup-common.ps1"

# Source token helper (look in same dir first, then parent)
$tokenHelperPath = Join-Path $PSScriptRoot "github-token-helper.ps1"
if (-not (Test-Path $tokenHelperPath)) {
    $tokenHelperPath = Join-Path (Split-Path $PSScriptRoot) "github-token-helper.ps1"
}
if (Test-Path $tokenHelperPath) {
    . $tokenHelperPath
}

# Configuration
$CodexRepo = "openai/codex"

function Install-Codex {
    Write-Info "Setting up Codex runtime..."

    # Detect platform
    Get-Platform

    # Map APM platform to Codex binary format
    switch ($script:DETECTED_PLATFORM) {
        "windows-x86_64" { $codexPlatform = "x86_64-pc-windows-msvc" }
        "windows-arm64"  { $codexPlatform = "aarch64-pc-windows-msvc" }
        default {
            Write-ErrorText "Unsupported platform: $script:DETECTED_PLATFORM"
            exit 1
        }
    }

    Initialize-ApmRuntimeDir

    $runtimeDir = Join-Path (Join-Path $env:USERPROFILE ".apm") "runtimes"
    $codexBinary = Join-Path $runtimeDir "codex.exe"
    $codexConfigDir = Join-Path $env:USERPROFILE ".codex"
    $codexConfig = Join-Path $codexConfigDir "config.toml"
    $tempDir = Join-Path $env:TEMP "apm-codex-install"

    if (-not (Test-Path $tempDir)) {
        New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
    }

    # Determine download URL
    $authHeaders = @{}
    if ($env:GITHUB_TOKEN) {
        $authHeaders["Authorization"] = "Bearer $($env:GITHUB_TOKEN)"
        Write-Info "Using authenticated GitHub API request (GITHUB_TOKEN)"
    } elseif ($env:GITHUB_APM_PAT) {
        $authHeaders["Authorization"] = "Bearer $($env:GITHUB_APM_PAT)"
        Write-Info "Using authenticated GitHub API request (GITHUB_APM_PAT)"
    } else {
        Write-Info "Using unauthenticated GitHub API request (60 requests/hour limit)"
    }

    if ($Version -eq "latest") {
        Write-Info "Fetching latest Codex release information..."
        $releaseUrl = "https://api.github.com/repos/$CodexRepo/releases/latest"
        $params = @{ Uri = $releaseUrl }
        if ($authHeaders.Count -gt 0) { $params["Headers"] = $authHeaders }

        try {
            $release = Invoke-RestMethod @params
            $latestTag = $release.tag_name
        } catch {
            Write-ErrorText "Failed to fetch latest release tag from GitHub API"
            exit 1
        }

        if (-not $latestTag) {
            Write-ErrorText "Failed to determine latest release tag"
            exit 1
        }

        Write-Info "Using Codex release: $latestTag"
        $downloadUrl = "https://github.com/$CodexRepo/releases/download/$latestTag/codex-$codexPlatform.exe.tar.gz"
    } else {
        $downloadUrl = "https://github.com/$CodexRepo/releases/download/$Version/codex-$codexPlatform.exe.tar.gz"
    }

    # Download archive
    $tarFile = Join-Path $tempDir "codex-$codexPlatform.exe.tar.gz"
    $dlHeaders = @{}
    if ($authHeaders.Count -gt 0) { $dlHeaders = $authHeaders }
    Save-File -Url $downloadUrl -Output $tarFile -Description "Codex binary archive" -Headers $dlHeaders

    # Extract (tar is available on Windows 10+)
    Write-Info "Extracting Codex binary..."
    Push-Location $tempDir
    tar -xzf $tarFile
    Pop-Location

    # Find extracted binary
    $extractedBinary = $null
    $candidates = @(
        (Join-Path $tempDir "codex.exe"),
        (Join-Path $tempDir "codex"),
        (Join-Path $tempDir "codex-$codexPlatform.exe"),
        (Join-Path $tempDir "codex-$codexPlatform")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            $extractedBinary = $candidate
            break
        }
    }

    if (-not $extractedBinary) {
        Write-ErrorText "Codex binary not found in extracted archive. Contents:"
        Get-ChildItem $tempDir | Format-Table Name
        exit 1
    }

    # Move to final location
    Move-Item -Force $extractedBinary $codexBinary

    # Clean up
    Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue

    Test-Binary $codexBinary "Codex"

    # Create configuration if not vanilla
    if (-not $Vanilla) {
        # Use centralized token management
        if (Get-Command Initialize-GitHubToken -ErrorAction SilentlyContinue) {
            Initialize-GitHubToken
        }

        if (-not (Test-Path $codexConfigDir)) {
            Write-Info "Creating Codex config directory: $codexConfigDir"
            New-Item -ItemType Directory -Force -Path $codexConfigDir | Out-Null
        }

        Write-Info "Creating Codex configuration for GitHub Models (APM default)..."

        $githubTokenVar = "GITHUB_TOKEN"
        if ($env:GITHUB_TOKEN) {
            Write-Info "Using GITHUB_TOKEN for GitHub Models authentication"
        } elseif ($env:GITHUB_APM_PAT) {
            $githubTokenVar = "GITHUB_APM_PAT"
            Write-WarningText "Using GITHUB_APM_PAT for GitHub Models (may not work if org-scoped)"
        } else {
            Write-Info "No GitHub token found - you'll need to set GITHUB_TOKEN"
        }

        @"
model_provider = "github-models"
model = "openai/gpt-4o"

[model_providers.github-models]
name = "GitHub Models"
base_url = "https://models.github.ai/inference/"
env_key = "$githubTokenVar"
wire_api = "responses"
"@ | Set-Content -Path $codexConfig -Encoding UTF8

        Write-Success "Codex configuration created at $codexConfig"
        Write-Info "Using Codex $Version."
        Write-Info "Override with: apm runtime setup codex --version <version> (e.g. 'latest')"
    } else {
        Write-Info "Vanilla mode: Skipping APM configuration"
    }

    Update-UserPath

    # Test installation
    Write-Info "Testing Codex installation..."
    try {
        $ver = & $codexBinary --version 2>&1
        Write-Success "Codex runtime installed successfully! Version: $ver"
    } catch {
        Write-WarningText "Codex binary installed but version check failed. It may still work."
    }

    Write-Host ""
    Write-Info "Next steps:"
    if (-not $Vanilla) {
        Write-Host "1. Set up your APM project: apm init my-project"
        Write-Host "2. Install MCP servers: apm install"
        Write-Host "3. Set your token: `$env:GITHUB_TOKEN = 'your_token_here'"
        Write-Host "4. Run: apm run start --param name=YourName"
        Write-Success "Codex installed and configured with GitHub Models!"
    } else {
        Write-Host "1. Configure Codex with your preferred provider"
        Write-Host "2. Then run with APM: apm run start"
    }
}

Install-Codex
