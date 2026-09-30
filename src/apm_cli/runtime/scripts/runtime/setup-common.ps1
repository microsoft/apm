# Common utilities for runtime setup scripts (Windows PowerShell)

$ErrorActionPreference = "Stop"

# Logging functions
function Write-Info { param([string]$Message) Write-Host "[INFO] $Message" -ForegroundColor Blue }
function Write-Success { param([string]$Message) Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-WarningText { param([string]$Message) Write-Host "[WARN] $Message" -ForegroundColor Yellow }
function Write-ErrorText { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red }

# Platform detection
function Get-Platform {
    $arch = [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture
    switch ($arch) {
        "X64"   { $script:DETECTED_PLATFORM = "windows-x86_64" }
        "Arm64" { $script:DETECTED_PLATFORM = "windows-arm64" }
        default {
            Write-ErrorText "Unsupported architecture: $arch"
            exit 1
        }
    }
    Write-Info "Detected platform: $script:DETECTED_PLATFORM"
}

# Create APM runtime directory
function Initialize-ApmRuntimeDir {
    $runtimeDir = Join-Path (Join-Path $env:USERPROFILE ".apm") "runtimes"
    if (-not (Test-Path $runtimeDir)) {
        Write-Info "Creating APM runtime directory: $runtimeDir"
        New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    }
}

# Add APM runtimes to user PATH if not already present
function Update-UserPath {
    $runtimeDir = Join-Path (Join-Path $env:USERPROFILE ".apm") "runtimes"

    # Update current session PATH
    if ($env:PATH -notlike "*$runtimeDir*") {
        $env:PATH = "$runtimeDir;$env:PATH"
        Write-Info "Added $runtimeDir to current session PATH"
    }

    # Persist to user PATH
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$runtimeDir*") {
        $newPath = "$runtimeDir;$userPath"
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Info "Added $runtimeDir to persistent user PATH"
    } else {
        Write-Info "PATH already configured for $runtimeDir"
    }

    Write-Success "Runtime binaries are now available!"
}

# Download file using Invoke-WebRequest
function Save-File {
    param(
        [string]$Url,
        [string]$Output,
        [string]$Description = "file",
        [hashtable]$Headers = @{}
    )

    Write-Info "Downloading $Description from $Url"
    $params = @{
        Uri             = $Url
        OutFile         = $Output
        UseBasicParsing = $true
    }
    if ($Headers.Count -gt 0) {
        $params["Headers"] = $Headers
    }
    Invoke-WebRequest @params
}

# Verify binary exists
function Test-Binary {
    param(
        [string]$BinaryPath,
        [string]$BinaryName
    )

    if (-not (Test-Path $BinaryPath)) {
        Write-ErrorText "$BinaryName binary not found at $BinaryPath"
        exit 1
    }

    Write-Success "$BinaryName binary installed and verified"
}
