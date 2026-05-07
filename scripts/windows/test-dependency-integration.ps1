# Extension to build isolation script for APM Dependencies Integration Testing
# Tests real dependency scenarios with actual GitHub repositories
# Used in CI pipeline for comprehensive dependency validation

$ErrorActionPreference = "Continue"

# --- Logging functions ---

function Write-DepInfo {
    param([string]$Message)
    Write-Host "i  $Message" -ForegroundColor Blue
}

function Write-DepSuccess {
    param([string]$Message)
    Write-Host "OK $Message" -ForegroundColor Green
}

function Write-DepError {
    param([string]$Message)
    Write-Host "FAIL $Message" -ForegroundColor Red
}

function Write-DepTestHeader {
    param([string]$Message)
    Write-Host "TEST $Message" -ForegroundColor Yellow
}

# --- Test real dependency installation ---

function Test-RealDependencyInstallation {
    param(
        [string]$TestDir,
        [string]$ApmBinary
    )

    Write-DepTestHeader "Testing real dependency installation with microsoft/apm-sample-package"

    Push-Location $TestDir
    try {
        # Create apm.yml with real dependency
        @"
name: dependency-test-project
version: 1.0.0
description: Test project for dependency integration testing
author: CI Test
target: copilot

dependencies:
  apm:
    - microsoft/apm-sample-package

scripts:
  start: "echo 'Project with apm-sample-package dependency loaded'"
"@ | Set-Content -Path "apm.yml" -Encoding UTF8

        # Test apm deps list (should show no dependencies initially)
        Write-DepInfo "Testing 'apm deps list' with no dependencies installed"
        $depsOutput = & $ApmBinary deps list 2>&1 | Out-String
        Write-Host "DEBUG: Actual output from 'apm deps list':"
        Write-Host "--- OUTPUT START ---"
        Write-Host $depsOutput
        Write-Host "--- OUTPUT END ---"
        if ($depsOutput -match "No APM dependencies installed") {
            Write-DepSuccess "Correctly shows no dependencies installed"
        } else {
            Write-DepError "Expected 'No APM dependencies installed' message"
            Write-DepError "Got: $depsOutput"
            return $false
        }

        # Test apm install (should download real dependency)
        Write-DepInfo "Testing 'apm install' with real GitHub dependency"
        & $ApmBinary install
        if ($LASTEXITCODE -ne 0) {
            Write-DepError "Failed to install real dependency"
            return $false
        }

        # Verify installation
        if (-not (Test-Path "apm_modules\microsoft\apm-sample-package")) {
            Write-DepError "Dependency not installed: apm_modules\microsoft\apm-sample-package not found"
            return $false
        }

        # Verify dependency structure
        if (-not (Test-Path "apm_modules\microsoft\apm-sample-package\apm.yml")) {
            Write-DepError "Dependency missing apm.yml"
            return $false
        }

        if (-not (Test-Path "apm_modules\microsoft\apm-sample-package\.apm")) {
            Write-DepError "Dependency missing .apm directory"
            return $false
        }

        # Check for expected prompt files
        if (-not (Test-Path "apm_modules\microsoft\apm-sample-package\.apm\prompts\design-review.prompt.md")) {
            Write-DepError "Dependency missing expected prompt file: .apm\prompts\design-review.prompt.md"
            return $false
        }

        Write-DepSuccess "Real dependency installation verified"

        # Test apm deps list (should now show installed dependency)
        Write-DepInfo "Testing 'apm deps list' with installed dependency"
        $depsOutput = & $ApmBinary deps list 2>&1 | Out-String
        if ($depsOutput -match "apm-sample-package") {
            Write-DepSuccess "Correctly shows installed dependency"
        } else {
            Write-DepError "Expected to see installed dependency in list"
            return $false
        }

        # Test apm deps tree
        Write-DepInfo "Testing 'apm deps tree'"
        $treeOutput = & $ApmBinary deps tree 2>&1 | Out-String
        if ($treeOutput -match "apm-sample-package") {
            Write-DepSuccess "Dependency tree shows installed dependency"
        } else {
            Write-DepError "Expected to see dependency in tree output"
            return $false
        }

        # Test apm deps info
        Write-DepInfo "Testing 'apm deps info apm-sample-package'"
        $infoOutput = & $ApmBinary deps info apm-sample-package 2>&1 | Out-String
        if ($infoOutput -match "apm-sample-package") {
            Write-DepSuccess "Dependency info command works"
        } else {
            Write-DepError "Expected dependency info to show package details"
            return $false
        }

        Write-DepSuccess "All real dependency tests passed"
        return $true
    } finally {
        Pop-Location
    }
}

# --- Test multi-dependency scenario ---

function Test-MultiDependencyScenario {
    param(
        [string]$TestDir,
        [string]$ApmBinary
    )

    Write-DepTestHeader "Testing multi-dependency scenario with both test repositories"

    Push-Location $TestDir
    try {
        # Create apm.yml with multiple dependencies
        @"
name: multi-dependency-test
version: 1.0.0
description: Test project for multi-dependency scenario
author: CI Test
target: copilot

dependencies:
  apm:
    - microsoft/apm-sample-package
    - github/awesome-copilot/skills/review-and-refactor

scripts:
  start: "echo 'Project with multiple dependencies loaded'"
"@ | Set-Content -Path "apm.yml" -Encoding UTF8

        # Clean any existing dependencies
        if (Test-Path "apm_modules") {
            Remove-Item -Recurse -Force "apm_modules" -ErrorAction SilentlyContinue
        }

        # Install multiple dependencies
        Write-DepInfo "Installing multiple real dependencies"
        & $ApmBinary install
        if ($LASTEXITCODE -ne 0) {
            Write-DepError "Failed to install multiple dependencies"
            return $false
        }

        # Verify both dependencies installed
        if (-not (Test-Path "apm_modules\microsoft\apm-sample-package")) {
            Write-DepError "First dependency not installed: apm-sample-package"
            return $false
        }

        if (-not (Test-Path "apm_modules\github\awesome-copilot\skills\review-and-refactor")) {
            Write-DepError "Second dependency not installed: github/awesome-copilot/skills/review-and-refactor"
            return $false
        }

        # Test deps list shows both
        $depsOutput = & $ApmBinary deps list 2>&1 | Out-String
        if ($depsOutput -notmatch "apm-sample-package") {
            Write-DepError "Multi-dependency list missing apm-sample-package"
            return $false
        }

        if ($depsOutput -notmatch "design-guidelines|apm-sample-package") {
            Write-DepError "Multi-dependency list missing design-guidelines"
            return $false
        }

        Write-DepSuccess "Multi-dependency scenario verified"
        return $true
    } finally {
        Pop-Location
    }
}

# --- Test dependency update workflow ---

function Test-DependencyUpdate {
    param(
        [string]$TestDir,
        [string]$ApmBinary
    )

    Write-DepTestHeader "Testing dependency update workflow"

    Push-Location $TestDir
    try {
        # Should have dependencies installed from previous test
        if (-not (Test-Path "apm_modules")) {
            Write-DepError "No dependencies found for update test"
            return $false
        }

        # Test update all dependencies
        Write-DepInfo "Testing 'apm deps update' for all dependencies"
        & $ApmBinary deps update
        if ($LASTEXITCODE -ne 0) {
            Write-DepError "Failed to update all dependencies"
            return $false
        }

        # Test update specific dependency
        Write-DepInfo "Testing 'apm deps update apm-sample-package'"
        & $ApmBinary deps update apm-sample-package
        if ($LASTEXITCODE -ne 0) {
            Write-DepError "Failed to update specific dependency"
            return $false
        }

        Write-DepSuccess "Dependency update workflow verified"
        return $true
    } finally {
        Pop-Location
    }
}

# --- Test dependency cleanup ---

function Test-DependencyCleanup {
    param(
        [string]$TestDir,
        [string]$ApmBinary
    )

    Write-DepTestHeader "Testing dependency cleanup"

    Push-Location $TestDir
    try {
        # Test deps clean
        Write-DepInfo "Testing 'apm deps clean'"
        "y" | & $ApmBinary deps clean
        if ($LASTEXITCODE -ne 0) {
            Write-DepError "Failed to clean dependencies"
            return $false
        }

        # Verify cleanup
        if (Test-Path "apm_modules") {
            Write-DepError "apm_modules directory still exists after cleanup"
            return $false
        }

        # Verify deps list shows no dependencies
        $depsOutput = & $ApmBinary deps list 2>&1 | Out-String
        Write-Host "DEBUG: Actual output from 'apm deps list' after cleanup:"
        Write-Host "--- OUTPUT START ---"
        Write-Host $depsOutput
        Write-Host "--- OUTPUT END ---"
        if ($depsOutput -match "No APM dependencies installed") {
            Write-DepSuccess "Correctly shows no dependencies after cleanup"
        } else {
            Write-DepError "Expected no dependencies after cleanup"
            Write-DepError "Got: $depsOutput"
            return $false
        }

        Write-DepSuccess "Dependency cleanup verified"
        return $true
    } finally {
        Pop-Location
    }
}

# --- Main function for dependency integration testing ---

function Test-DependencyIntegration {
    param(
        [Parameter(Mandatory)]
        [string]$BinaryPath
    )

    Write-DepInfo "=== APM Dependencies Integration Testing ==="
    Write-DepInfo "Testing with real GitHub repositories:"
    Write-DepInfo "  - microsoft/apm-sample-package"
    Write-DepInfo "  - github/awesome-copilot/skills/review-and-refactor"

    # Create isolated test directory
    $testDir = Join-Path $env:TEMP "apm-dep-test-$PID"
    New-Item -ItemType Directory -Path $testDir -Force | Out-Null

    # Check for GitHub token
    if (-not $env:GITHUB_CLI_PAT -and -not $env:GITHUB_TOKEN) {
        Write-DepError "GitHub token required for dependency testing"
        Write-DepInfo "Set GITHUB_CLI_PAT or GITHUB_TOKEN environment variable"
        return $false
    }

    try {
        # Run dependency tests in sequence
        if (-not (Test-RealDependencyInstallation -TestDir $testDir -ApmBinary $BinaryPath)) { return $false }
        if (-not (Test-MultiDependencyScenario -TestDir $testDir -ApmBinary $BinaryPath)) { return $false }
        if (-not (Test-DependencyUpdate -TestDir $testDir -ApmBinary $BinaryPath)) { return $false }
        if (-not (Test-DependencyCleanup -TestDir $testDir -ApmBinary $BinaryPath)) { return $false }

        Write-DepSuccess "=== All dependency integration tests passed! ==="
        return $true
    } finally {
        # Cleanup
        if (Test-Path $testDir) {
            Remove-Item -Recurse -Force $testDir -ErrorAction SilentlyContinue
        }
    }
}

# If run directly (not dot-sourced)
if ($MyInvocation.InvocationName -ne ".") {
    if ($args.Count -lt 1) {
        Write-DepError "Usage: .\test-dependency-integration.ps1 <apm_binary_path>"
        exit 1
    }

    $result = Test-DependencyIntegration -BinaryPath $args[0]
    if (-not $result) { exit 1 }
}
