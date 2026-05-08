# Integration testing script for Windows CI and local environments
# PowerShell equivalent of test-integration.sh
#
# Tests comprehensive runtime scenarios and edge cases:
#   - pytest-based E2E scenarios with error handling
#   - Hero scenario validation (zero-config, guardrailing)
#   - MCP registry integration
#   - APM Dependencies with real repositories
#
# - CI mode: Uses pre-built artifacts from build job
# - Local mode: Builds binary, runs integration tests

param(
    [switch]$SkipBuild,
    [switch]$SkipRuntimes
)

$ErrorActionPreference = "Stop"

# Source the GitHub token management helper
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$tokenHelper = Join-Path $ScriptDir "github-token-helper.ps1"
if (Test-Path $tokenHelper) {
    . $tokenHelper
}

#region Logging
function Write-Info { param([string]$Message) Write-Host "[INFO] $Message" -ForegroundColor Blue }
function Write-Success { param([string]$Message) Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-ErrorText { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red }
#endregion

#region Prerequisites
function Test-Prerequisites {
    Write-Info "Checking prerequisites..."

    if (Get-Command Initialize-GitHubToken -ErrorAction SilentlyContinue) {
        Initialize-GitHubToken
        Write-Success "GitHub tokens configured"
    }

    if ($env:GITHUB_APM_PAT) { Write-Success "GITHUB_APM_PAT is set (APM module access)" }
    if ($env:GITHUB_TOKEN)   { Write-Success "GITHUB_TOKEN is set (GitHub Models access)" }
}
#endregion

#region Platform and Environment Detection
function Get-BinaryName {
    $arch = [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture
    switch ($arch) {
        "X64"   { return "apm-windows-x86_64" }
        "Arm64" { return "apm-windows-x86_64" }  # x86_64 emulation on ARM64
        default {
            Write-ErrorText "Unsupported architecture: $arch"
            exit 1
        }
    }
}

function Find-ExistingBinary {
    param([string]$BinaryName)

    $binaryPath = Join-Path (Join-Path (Join-Path "." "dist") $BinaryName) "apm.exe"
    if (Test-Path $binaryPath) {
        Write-Info "Found existing binary: $binaryPath (CI mode)"
        return $true
    }

    # Also check for directory-style artifact (download-artifact extracts flat)
    $flatPath = Join-Path (Join-Path "." $BinaryName) "apm.exe"
    if (Test-Path $flatPath) {
        Write-Info "Found existing binary: $flatPath (CI mode)"
        return $true
    }

    Write-Info "No existing binary found, will build locally"
    return $false
}
#endregion

#region Binary Build and Setup
function Build-Binary {
    param([string]$BinaryName)

    Write-Info "=== Building APM binary (local mode) ==="

    Write-Info "Installing build dependencies..."
    uv sync --extra dev --extra build

    Write-Info "Building binary with PyInstaller..."
    uv run pyinstaller build/apm.spec --noconfirm

    $binaryPath = Join-Path (Join-Path (Join-Path "." "dist") $BinaryName) "apm.exe"
    if (-not (Test-Path $binaryPath)) {
        Write-ErrorText "Binary not found after build: $binaryPath"
        exit 1
    }

    Write-Success "Binary built: $binaryPath"
}

function Initialize-BinaryForTesting {
    param([string]$BinaryName)

    Write-Info "=== Setting up binary for testing ==="

    $binaryDir = Join-Path (Join-Path (Get-Location) "dist") $BinaryName
    if (-not (Test-Path (Join-Path $binaryDir "apm.exe"))) {
        # Check flat layout from download-artifact
        $binaryDir = Join-Path (Get-Location) $BinaryName
    }

    if (-not (Test-Path (Join-Path $binaryDir "apm.exe"))) {
        Write-ErrorText "Cannot find apm.exe in $binaryDir"
        exit 1
    }

    # Add binary directory to PATH for this session
    $env:PATH = "$binaryDir;$env:PATH"

    # Verify setup
    $apmPath = Get-Command apm -ErrorAction SilentlyContinue
    if (-not $apmPath) {
        Write-ErrorText "APM not found in PATH after setup"
        exit 1
    }

    $version = & apm --version 2>&1
    Write-Success "APM binary ready for testing: $version"
}
#endregion

#region Runtime Setup
function Initialize-Runtimes {
    Write-Info "=== Setting up runtimes for integration tests ==="

    Write-Info "Setting up GitHub Copilot CLI runtime..."
    & apm runtime setup copilot
    if ($LASTEXITCODE -ne 0) { Write-ErrorText "Failed to set up Copilot runtime"; exit 1 }

    Write-Info "Setting up Codex runtime..."
    & apm runtime setup codex
    if ($LASTEXITCODE -ne 0) { Write-ErrorText "Failed to set up Codex runtime"; exit 1 }

    Write-Info "Setting up LLM runtime..."
    & apm runtime setup llm
    if ($LASTEXITCODE -ne 0) { Write-ErrorText "Failed to set up LLM runtime"; exit 1 }

    # Add runtime paths to session
    $runtimeDir = Join-Path (Join-Path $env:USERPROFILE ".apm") "runtimes"
    $env:PATH = "$runtimeDir;$env:PATH"

    Write-Success "All runtimes configured (Copilot, Codex, LLM)"
}
#endregion

#region Integration Tests
function Invoke-IntegrationTests {
    Write-Info "=== Running integration tests (mirroring CI) ==="
    Write-Info "Testing comprehensive runtime scenarios:"
    Write-Info "  - Zero-config auto-install (Hero Scenario 1)"
    Write-Info "  - 2-minute guardrailing (Hero Scenario 2)"
    Write-Info "  - MCP registry integration"
    Write-Info "  - APM Dependencies with real repositories"

    $env:APM_E2E_TESTS = "1"
    $env:PYTHONUTF8 = "1"

    Write-Info "Environment:"
    Write-Host "  APM_E2E_TESTS: $env:APM_E2E_TESTS"
    Write-Host "  GITHUB_TOKEN: $(if ($env:GITHUB_TOKEN) { '(set)' } else { '(not set)' })"
    Write-Host "  GITHUB_APM_PAT: $(if ($env:GITHUB_APM_PAT) { '(set)' } else { '(not set)' })"
    Write-Host "  ADO_APM_PAT: $(if ($env:ADO_APM_PAT) { '(set)' } else { '(not set)' })"

    # Hero Scenario 1: Zero-config auto-install
    Write-Info "Running HERO SCENARIO 1: Zero-config auto-install test..."
    pytest tests/integration/test_auto_install_e2e.py -v -s --tb=short
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorText "Zero-config auto-install tests failed!"
        exit 1
    }
    Write-Success "Zero-config auto-install tests passed!"

    # Hero Scenario 2: 2-minute guardrailing
    Write-Info "Running HERO SCENARIO 2: 2-minute guardrailing test..."
    pytest tests/integration/test_guardrailing_hero_e2e.py -v -s --tb=short
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorText "2-minute guardrailing tests failed!"
        exit 1
    }
    Write-Success "2-minute guardrailing tests passed!"

    # MCP registry E2E tests
    Write-Info "Running MCP registry E2E tests..."
    pytest tests/integration/test_mcp_registry_e2e.py -v -s --tb=short
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorText "MCP registry tests failed!"
        exit 1
    }
    Write-Success "MCP registry tests passed!"

    # APM Dependencies integration tests
    Write-Info "Running APM Dependencies integration tests..."
    pytest tests/integration/test_apm_dependencies.py -v -s --tb=short -m integration
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorText "APM Dependencies integration tests failed!"
        exit 1
    }
    Write-Success "APM Dependencies integration tests passed!"

    # Azure DevOps E2E tests (conditional)
    if ($env:ADO_APM_PAT) {
        Write-Info "Running Azure DevOps E2E tests..."
        pytest tests/integration/test_ado_e2e.py -v -s --tb=short
        if ($LASTEXITCODE -ne 0) {
            Write-ErrorText "Azure DevOps E2E tests failed!"
            exit 1
        }
        Write-Success "Azure DevOps E2E tests passed!"
    } else {
        Write-Info "Skipping Azure DevOps E2E tests (ADO_APM_PAT not set)"
    }

    # #1212 anti-regression: ADO --update preflight bearer-fallback.
    # The Python E2E (test_ado_preflight_bearer_fallback_e2e.py) is POSIX-only
    # (relies on a fake `git` shim invoked via shebang); on Windows it
    # `skipif`s. A native Windows variant is tracked as follow-up; until it
    # exists, the unit suite (`tests/unit/install/test_pipeline_auth_preflight.py`)
    # already covers the preflight bearer-fallback contract on every platform.
    Write-Info "Skipping #1212 ADO preflight E2E on Windows (POSIX-only fake shim); unit suite covers the contract."

    Write-Success "All integration test suites completed successfully!"
}
#endregion

#region Main
Write-Host "APM CLI Integration Testing - Windows" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

Test-Prerequisites

$binaryName = Get-BinaryName
$hasExisting = Find-ExistingBinary -BinaryName $binaryName

if (-not $hasExisting -and -not $SkipBuild) {
    Build-Binary -BinaryName $binaryName
} elseif (-not $hasExisting -and $SkipBuild) {
    Write-ErrorText "No binary found and -SkipBuild specified"
    exit 1
}

Initialize-BinaryForTesting -BinaryName $binaryName

if (-not $SkipRuntimes) {
    Initialize-Runtimes
}

Invoke-IntegrationTests

Write-Success "All integration tests completed successfully!"
#endregion
