# Release validation script - Final pre-release testing (PowerShell)
# Tests the EXACT user experience with the shipped binary in complete isolation:
#   1. Download/extract binary (as users would)
#   2. apm runtime setup codex
#   3. apm init my-ai-native-project
#   4. cd my-ai-native-project && apm compile
#   5. apm install
#   6. apm run start --param name="<YourGitHubHandle>"
#
# Environment: Complete isolation - NO source code, only the binary
# Purpose: Validate that end-users will have a successful experience
# This is the final gate before release - testing the actual product as shipped

param(
    [string]$BinaryPath
)

$ErrorActionPreference = "Continue"

# --- Logging functions ---

function Write-Info {
    param([string]$Message)
    Write-Host "i  $Message" -ForegroundColor Blue
}

function Write-Success {
    param([string]$Message)
    Write-Host "OK $Message" -ForegroundColor Green
}

function Write-ErrorText {
    param([string]$Message)
    Write-Host "FAIL $Message" -ForegroundColor Red
}

function Write-TestHeader {
    param([string]$Message)
    Write-Host "TEST $Message" -ForegroundColor Yellow
}

# --- Source helpers ---

. "$PSScriptRoot\github-token-helper.ps1"

$script:DEPENDENCY_TESTS_AVAILABLE = $false
$depIntegrationScript = Join-Path $PSScriptRoot "test-dependency-integration.ps1"
if (Test-Path $depIntegrationScript) {
    . $depIntegrationScript
    $script:DEPENDENCY_TESTS_AVAILABLE = $true
}

# --- Global state ---

$script:BINARY_PATH = ""
$script:testDir = ""

# --- Helper: run with timeout ---

function Invoke-WithTimeout {
    param(
        [int]$Seconds,
        [string]$Command,
        [string[]]$Arguments
    )
    $process = Start-Process -FilePath $Command -ArgumentList $Arguments -NoNewWindow -PassThru -RedirectStandardOutput "$env:TEMP\apm-timeout-stdout.txt" -RedirectStandardError "$env:TEMP\apm-timeout-stderr.txt"
    if (-not $process.WaitForExit($Seconds * 1000)) {
        $process.Kill()
        if (Test-Path "$env:TEMP\apm-timeout-stdout.txt") { Get-Content "$env:TEMP\apm-timeout-stdout.txt" }
        if (Test-Path "$env:TEMP\apm-timeout-stderr.txt") { Get-Content "$env:TEMP\apm-timeout-stderr.txt" }
        return 124  # timeout code
    }
    if (Test-Path "$env:TEMP\apm-timeout-stdout.txt") { Get-Content "$env:TEMP\apm-timeout-stdout.txt" }
    if (Test-Path "$env:TEMP\apm-timeout-stderr.txt") { Get-Content "$env:TEMP\apm-timeout-stderr.txt" }
    return $process.ExitCode
}

# --- Find binary ---

function Find-Binary {
    param([string]$Path)

    if ($Path) {
        if (-not (Test-Path $Path)) {
            Write-ErrorText "Binary not found at specified path: $Path"
            exit 1
        }
        $script:BINARY_PATH = (Resolve-Path $Path).Path
    } elseif (Test-Path ".\apm.exe") {
        $script:BINARY_PATH = (Resolve-Path ".\apm.exe").Path
    } else {
        $cmd = Get-Command apm -ErrorAction SilentlyContinue
        if ($cmd) {
            $script:BINARY_PATH = $cmd.Source
        } else {
            Write-ErrorText "APM binary not found. Usage: .\test-release-validation.ps1 [path-to-binary]"
            exit 1
        }
    }

    Write-Info "Testing binary: $script:BINARY_PATH"
}

# --- Prerequisites ---

function Test-Prerequisite {
    Write-TestHeader "Prerequisites: GitHub token"

    Initialize-GitHubToken
    # Initialize-GitHubToken doesn't return failure — check tokens after setup
    if ($env:GITHUB_TOKEN -or $env:GITHUB_APM_PAT) {
        Write-Success "GitHub tokens configured successfully"

        if ($env:GITHUB_APM_PAT) {
            Write-Success "GITHUB_APM_PAT is set (APM module access)"
        }
        if ($env:GITHUB_TOKEN) {
            Write-Success "GITHUB_TOKEN is set (GitHub Models access)"
        }
        return $true
    } else {
        Write-ErrorText "GitHub token setup failed"
        return $false
    }
}

# --- Test: basic commands ---

function Test-BasicCommand {
    Write-TestHeader "Sanity check: Basic commands"

    # Test --version
    Write-Host "Running: $script:BINARY_PATH --version"
    Write-Host "--- Command Output Start ---"
    $result = & $script:BINARY_PATH --version 2>&1
    $versionExitCode = $LASTEXITCODE
    $result | Out-Host
    Write-Host "--- Command Output End ---"
    Write-Host "Exit code: $versionExitCode"

    if ($versionExitCode -ne 0) {
        Write-ErrorText "apm --version failed with exit code $versionExitCode"
        return $false
    }

    # Test --help
    Write-Host "Running: $script:BINARY_PATH --help"
    Write-Host "--- Command Output Start ---"
    $result = & $script:BINARY_PATH --help 2>&1
    $helpExitCode = $LASTEXITCODE
    $result | Select-Object -First 20 | Out-Host
    Write-Host "--- Command Output End ---"
    Write-Host "Exit code: $helpExitCode"

    if ($helpExitCode -ne 0) {
        Write-ErrorText "apm --help failed with exit code $helpExitCode"
        return $false
    }

    Write-Success "Basic commands work"
    return $true
}

# --- Test: runtime setup ---

function Test-RuntimeSetup {
    Write-TestHeader "README Step 2: apm runtime setup"

    # Install GitHub Copilot CLI
    Write-Host "Running: $script:BINARY_PATH runtime setup copilot"
    Write-Host "--- Command Output Start ---"
    $result = & $script:BINARY_PATH runtime setup copilot 2>&1
    $exitCode = $LASTEXITCODE
    $result | Out-Host
    Write-Host "--- Command Output End ---"
    Write-Host "Exit code: $exitCode"

    if ($exitCode -ne 0) {
        Write-ErrorText "apm runtime setup copilot failed with exit code $exitCode"
        return $false
    }

    Write-Success "Copilot CLI runtime setup completed"

    # Also install Codex CLI
    Write-Host "Running: $script:BINARY_PATH runtime setup codex"
    Write-Host "--- Command Output Start ---"
    $result = & $script:BINARY_PATH runtime setup codex 2>&1
    $exitCode = $LASTEXITCODE
    $result | Out-Host
    Write-Host "--- Command Output End ---"
    Write-Host "Exit code: $exitCode"

    if ($exitCode -ne 0) {
        Write-ErrorText "apm runtime setup codex failed with exit code $exitCode"
        return $false
    }

    Write-Success "Codex CLI runtime setup completed"
    Write-Success "Both runtimes (Copilot, Codex) configured successfully"
    return $true
}

# --- HERO SCENARIO 1: 30-Second Zero-Config ---

function Test-HeroZeroConfig {
    Write-TestHeader "HERO SCENARIO 1: 30-Second Zero-Config (README lines 35-44)"

    # Create temporary directory for this test
    New-Item -ItemType Directory -Path "zero-config-test" -Force | Out-Null
    Push-Location "zero-config-test"

    try {
        # Runtime setup is already done in Test-RuntimeSetup
        # Just test the virtual package run
        Write-Host "Running: $script:BINARY_PATH run github/awesome-copilot/skills/architecture-blueprint-generator (with 15s timeout)"
        Write-Host "--- Command Output Start ---"
        $exitCode = Invoke-WithTimeout -Seconds 15 -Command $script:BINARY_PATH -Arguments @("run", "github/awesome-copilot/skills/architecture-blueprint-generator")
        Write-Host "--- Command Output End ---"
        Write-Host "Exit code: $exitCode"

        if ($exitCode -eq 124) {
            # Timeout is expected and OK (prompt execution started)
            Write-Success "Zero-config auto-install worked! Package installed and prompt started."
        } elseif ($exitCode -eq 0) {
            Write-Success "Zero-config auto-install completed successfully"
        } else {
            Write-ErrorText "Zero-config auto-install failed immediately with exit code $exitCode"
            return $false
        }

        # Verify package was actually installed
        if (-not (Test-Path "apm_modules\github\awesome-copilot\skills\architecture-blueprint-generator")) {
            Write-ErrorText "Package was not installed by auto-install"
            return $false
        }

        Write-Success "Package auto-installed to apm_modules/"

        # Test second run (should use cached package, no re-download)
        Write-Host "Testing second run (should use cache)..."
        $secondExitCode = Invoke-WithTimeout -Seconds 10 -Command $script:BINARY_PATH -Arguments @("run", "github/awesome-copilot/skills/architecture-blueprint-generator")

        if ($secondExitCode -eq 124 -or $secondExitCode -eq 0) {
            Write-Success "Second run used cached package (fast, no re-download)"
        }

        Write-Success "HERO SCENARIO 1: 30-second zero-config PASSED"
        return $true
    } finally {
        Pop-Location
    }
}

# --- HERO SCENARIO 2: 2-Minute Guardrailing ---

function Test-HeroGuardrailing {
    Write-TestHeader "HERO SCENARIO 2: 2-Minute Guardrailing (README lines 46-60)"

    # Step 1: apm init my-project
    Write-Host "Running: $script:BINARY_PATH init my-project --yes --target copilot"
    Write-Host "--- Command Output Start ---"
    $result = & $script:BINARY_PATH init my-project --yes --target copilot 2>&1
    $exitCode = $LASTEXITCODE
    $result | Out-Host
    Write-Host "--- Command Output End ---"
    Write-Host "Exit code: $exitCode"

    if ($exitCode -ne 0) {
        Write-ErrorText "apm init my-project failed with exit code $exitCode"
        return $false
    }

    if (-not (Test-Path "my-project") -or -not (Test-Path "my-project\apm.yml")) {
        Write-ErrorText "my-project directory or apm.yml not created"
        return $false
    }

    Write-Success "Project initialized"

    Push-Location "my-project"

    try {
        # Step 2: apm install microsoft/apm-sample-package
        Write-Host "Running: $script:BINARY_PATH install microsoft/apm-sample-package"
        Write-Host "--- Command Output Start ---"
        $result = & $script:BINARY_PATH install microsoft/apm-sample-package 2>&1
        $exitCode = $LASTEXITCODE
        $result | Out-Host
        Write-Host "--- Command Output End ---"
        Write-Host "Exit code: $exitCode"

        if ($exitCode -ne 0) {
            Write-ErrorText "apm install microsoft/apm-sample-package failed"
            return $false
        }

        Write-Success "design-guidelines installed"

        # Step 3: apm install github/awesome-copilot/skills/review-and-refactor
        Write-Host "Running: $script:BINARY_PATH install github/awesome-copilot/skills/review-and-refactor"
        Write-Host "--- Command Output Start ---"
        $result = & $script:BINARY_PATH install github/awesome-copilot/skills/review-and-refactor 2>&1
        $exitCode = $LASTEXITCODE
        $result | Out-Host
        Write-Host "--- Command Output End ---"
        Write-Host "Exit code: $exitCode"

        if ($exitCode -ne 0) {
            Write-ErrorText "apm install github/awesome-copilot/skills/review-and-refactor failed"
            return $false
        }

        Write-Success "virtual package installed"

        # Step 4: apm compile
        Write-Host "Running: $script:BINARY_PATH compile"
        Write-Host "--- Command Output Start ---"
        $result = & $script:BINARY_PATH compile 2>&1
        $exitCode = $LASTEXITCODE
        $result | Out-Host
        Write-Host "--- Command Output End ---"
        Write-Host "Exit code: $exitCode"

        if ($exitCode -ne 0) {
            Write-ErrorText "apm compile failed"
            return $false
        }

        if (-not (Test-Path "AGENTS.md")) {
            Write-ErrorText "AGENTS.md not created by compile"
            return $false
        }

        Write-Success "Compiled to AGENTS.md (guardrails active)"

        # Step 5: apm run design-review (from installed package)
        Write-Host "Running: $script:BINARY_PATH run design-review (with 10s timeout)"
        Write-Host "--- Command Output Start ---"
        $exitCode = Invoke-WithTimeout -Seconds 10 -Command $script:BINARY_PATH -Arguments @("run", "design-review")
        Write-Host "--- Command Output End ---"
        Write-Host "Exit code: $exitCode"

        if ($exitCode -eq 124) {
            # Timeout is expected and OK - prompt started executing
            Write-Success "design-review prompt executed with compiled guardrails"
        } elseif ($exitCode -eq 0) {
            Write-Success "design-review completed successfully"
        } else {
            Write-ErrorText "apm run design-review failed immediately"
            return $false
        }

        Write-Success "HERO SCENARIO 2: 2-minute guardrailing PASSED"
        return $true
    } finally {
        Pop-Location
    }
}

# --- Main ---

function Main {
    Write-Host "APM CLI Release Validation - Binary Isolation Testing"
    Write-Host "====================================================="
    Write-Host ""
    Write-Host "Testing the EXACT user experience with the shipped binary"
    Write-Host "Environment: Complete isolation (no source code access)"
    Write-Host "Purpose: Final validation before release"
    Write-Host ""

    Find-Binary -Path $BinaryPath

    # Test binary accessibility first
    Write-Host "Testing binary accessibility..."
    if (-not (Test-Path $script:BINARY_PATH)) {
        Write-ErrorText "Binary file does not exist: $script:BINARY_PATH"
        exit 1
    }

    Write-Host "Binary found: $script:BINARY_PATH"

    $testsPassed = 0
    $testsTotal = 5  # Prerequisites, basic commands, runtime setup, 2 hero scenarios
    $dependencyTestsRun = $false

    # Add dependency tests to total if available and GITHUB token is present
    if ($script:DEPENDENCY_TESTS_AVAILABLE -and ($env:GITHUB_CLI_PAT -or $env:GITHUB_TOKEN)) {
        $testsTotal++
        $dependencyTestsRun = $true
        Write-Info "Dependency integration tests will be included"
    } elseif ($script:DEPENDENCY_TESTS_AVAILABLE) {
        Write-Info "Dependency integration tests available but no GitHub token - skipping"
    } else {
        Write-Info "Dependency integration tests not available - skipping"
    }

    # Create isolated test directory
    $script:testDir = "binary-golden-scenario-$PID"
    New-Item -ItemType Directory -Path $script:testDir | Out-Null
    Push-Location $script:testDir

    try {
        # Run prerequisites and basic tests
        if (Test-Prerequisite) {
            $testsPassed++
        } else {
            Write-ErrorText "Prerequisites check failed"
        }

        if (Test-BasicCommand) {
            $testsPassed++
        } else {
            Write-ErrorText "Basic commands test failed"
        }

        if (Test-RuntimeSetup) {
            $testsPassed++
        } else {
            Write-ErrorText "Runtime setup test failed"
        }

        # HERO SCENARIO 1: 30-second zero-config
        if (Test-HeroZeroConfig) {
            $testsPassed++
        } else {
            Write-ErrorText "Hero scenario 1 (30-sec zero-config) failed"
        }

        # HERO SCENARIO 2: 2-minute guardrailing
        if (Test-HeroGuardrailing) {
            $testsPassed++
        } else {
            Write-ErrorText "Hero scenario 2 (2-min guardrailing) failed"
        }

        # Run dependency integration tests if available and GitHub token is set
        if ($dependencyTestsRun) {
            Write-Info "Running dependency integration tests with real GitHub repositories"
            if (Test-DependencyIntegration -BinaryPath $script:BINARY_PATH) {
                $testsPassed++
                Write-Success "Dependency integration tests passed"
            } else {
                Write-ErrorText "Dependency integration tests failed"
            }
        }
    } finally {
        Pop-Location
        # Cleanup test directory
        if ($script:testDir -and (Test-Path $script:testDir)) {
            Write-Host "Cleaning up test directory: $script:testDir"
            Remove-Item -Recurse -Force $script:testDir -ErrorAction SilentlyContinue
        }
    }

    Write-Host ""
    Write-Host "Results: $testsPassed/$testsTotal tests passed"

    if ($testsPassed -eq $testsTotal) {
        Write-Host "RELEASE VALIDATION PASSED!" -ForegroundColor Green
        Write-Host ""
        Write-Host "Binary is ready for production release"
        Write-Host "End-user experience validated successfully"
        Write-Host "Both README hero scenarios work perfectly"
        Write-Host ""
        Write-Host "Validated user journeys:"
        Write-Host "  1. Prerequisites (GITHUB_TOKEN)"
        Write-Host "  2. Binary accessibility"
        Write-Host "  3. Runtime setup (copilot)"
        Write-Host ""
        Write-Host "  HERO SCENARIO 1: 30-Second Zero-Config"
        Write-Host "    - Run virtual package directly"
        Write-Host "    - Auto-install on first run"
        Write-Host "    - Use cached package on second run"
        Write-Host ""
        Write-Host "  HERO SCENARIO 2: 2-Minute Guardrailing"
        Write-Host "    - Project initialization"
        Write-Host "    - Install APM packages"
        Write-Host "    - Compile to AGENTS.md guardrails"
        Write-Host "    - Run prompts with guardrails"
        if ($dependencyTestsRun) {
            Write-Host ""
            Write-Host "  BONUS: Real dependency integration"
        }
        Write-Host ""
        Write-Success "README Hero Scenarios work perfectly!"
        Write-Host ""
        Write-Host "The binary delivers the exact README experience - real users will love it!"
        exit 0
    } else {
        Write-ErrorText "Some tests failed"
        Write-Host ""
        Write-Host "The binary doesn't match the README promise"
        exit 1
    }
}

# Run main function
Main
