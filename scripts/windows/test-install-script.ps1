# Windows-only end-to-end test for install.ps1.
#
# Covers the fixes for issue microsoft/apm#1389:
#   1. SHA256 verification works on hardened hosts where Get-FileHash is not
#      autoloaded (.NET stream fallback via System.Security.Cryptography).
#   2. The binary smoke test runs from the final install root (under
#      %LOCALAPPDATA%\Programs\apm\releases\...), NOT from %TEMP%, so it
#      survives AppLocker / App Control for Business policies that block
#      executable launch from user-writable temp paths.
#   3. The shim written to APM_INSTALL_DIR points at the promoted release
#      directory and the temp staging area is cleaned up.
#
# Designed to run on the windows-latest GitHub Actions runner. Performs a
# real install of a pinned APM release into an isolated test prefix and
# leaves the developer's existing apm install untouched.

param(
    [string]$PinnedVersion = "v0.13.0"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Resolve-Path (Join-Path $ScriptDir "..\..")
$InstallScript = Join-Path $RepoRoot "install.ps1"

function Write-Info    { param([string]$M) Write-Host "[INFO] $M"   -ForegroundColor Blue }
function Write-Success { param([string]$M) Write-Host "[OK] $M"     -ForegroundColor Green }
function Write-Step    { param([string]$M) Write-Host "[STEP] $M"   -ForegroundColor Cyan }
function Write-Fail    { param([string]$M) Write-Host "[FAIL] $M"   -ForegroundColor Red }

$Script:Failures = @()
function Assert-True {
    param([bool]$Condition, [string]$Message)
    if ($Condition) {
        Write-Success $Message
    } else {
        Write-Fail $Message
        $Script:Failures += $Message
    }
}

# ---------------------------------------------------------------------------
# Test 1: Get-Sha256Hex function falls back to .NET when Get-FileHash is gone.
# ---------------------------------------------------------------------------

function Test-Sha256Fallback {
    Write-Step "Test 1: Get-Sha256Hex .NET fallback works without Get-FileHash"

    if (-not (Test-Path $InstallScript)) {
        Write-Fail "install.ps1 not found at $InstallScript"
        $Script:Failures += "install.ps1 missing"
        return
    }

    $content = Get-Content $InstallScript -Raw
    $pattern = '(?s)function Get-Sha256Hex\s*\{.*?\n\}'
    $match = [regex]::Match($content, $pattern)
    if (-not $match.Success) {
        Write-Fail "Could not extract Get-Sha256Hex function from install.ps1"
        $Script:Failures += "Get-Sha256Hex extraction"
        return
    }

    $tempFile = [System.IO.Path]::GetTempFileName()
    try {
        Set-Content -Path $tempFile -Value "the quick brown fox" -NoNewline -Encoding ASCII
        $expected = (Get-FileHash -Path $tempFile -Algorithm SHA256).Hash.ToLower()

        # Run the extracted function in an isolated child pwsh with the
        # PSModulePath cleared and Microsoft.PowerShell.Utility removed,
        # which simulates a hardened host where Get-Command Get-FileHash
        # returns nothing and the fallback must take over.
        $childScript = @"
`$ErrorActionPreference = 'Stop'
`$env:PSModulePath = ''
Remove-Module Microsoft.PowerShell.Utility -Force -ErrorAction SilentlyContinue
$($match.Value)
Write-Output (Get-Sha256Hex -Path '$tempFile')
"@
        $childScriptPath = [System.IO.Path]::GetTempFileName() + ".ps1"
        Set-Content -Path $childScriptPath -Value $childScript -Encoding UTF8
        try {
            $actual = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $childScriptPath 2>&1
            $actualStr = ($actual | Out-String).Trim().ToLower()
            Assert-True ($actualStr -eq $expected) "SHA256 fallback returns expected hash (expected $expected, got $actualStr)"
        } finally {
            Remove-Item $childScriptPath -ErrorAction SilentlyContinue
        }
    } finally {
        Remove-Item $tempFile -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Test 2: Structural assertion that binary test happens from the final
# release tree, not from the system temp dir. We assert this by reading
# install.ps1 and confirming that the move-then-test ordering is in place.
# ---------------------------------------------------------------------------

function Test-MoveThenTestOrdering {
    Write-Step "Test 2: install.ps1 moves bundle out of temp before running binary test"

    $content = Get-Content $InstallScript -Raw

    $stageIdx = $content.IndexOf("Move-Item -Path `$packageDir -Destination `$stagingDir")
    $testIdx  = $content.IndexOf("& `$stagedExe --version")

    Assert-True ($stageIdx -gt 0) "Found staging Move-Item in install.ps1"
    Assert-True ($testIdx  -gt 0) "Found binary smoke test in install.ps1"
    Assert-True (($stageIdx -gt 0) -and ($testIdx -gt 0) -and ($stageIdx -lt $testIdx)) "Binary test runs AFTER bundle is moved out of temp"
}

# ---------------------------------------------------------------------------
# Test 3: Run install.ps1 end-to-end into an isolated prefix.
# ---------------------------------------------------------------------------

function Test-EndToEndInstall {
    Write-Step "Test 3: End-to-end install of APM $PinnedVersion into isolated prefix"

    $testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("apm-install-test-" + [System.Guid]::NewGuid().ToString("N"))
    $binDir   = Join-Path $testRoot "bin"
    $tmpDir   = Join-Path $testRoot "tmp"
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

    $savedVersion       = $env:VERSION
    $savedInstallDir    = $env:APM_INSTALL_DIR
    $savedTempDir       = $env:APM_TEMP_DIR
    $savedSkipChecksum  = $env:APM_SKIP_CHECKSUM

    try {
        $env:VERSION         = $PinnedVersion
        $env:APM_INSTALL_DIR = $binDir
        $env:APM_TEMP_DIR    = $tmpDir
        # Real pinned releases have a .sha256 sidecar; keep checksum verification on
        # so we also exercise Get-Sha256Hex against the real download.
        Remove-Item Env:APM_SKIP_CHECKSUM -ErrorAction SilentlyContinue

        Write-Info "Running install.ps1 (VERSION=$PinnedVersion, APM_INSTALL_DIR=$binDir, APM_TEMP_DIR=$tmpDir)"
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $InstallScript
        $exitCode = $LASTEXITCODE
        Assert-True ($exitCode -eq 0) "install.ps1 exits 0 (got $exitCode)"

        $shim = Join-Path $binDir "apm.cmd"
        Assert-True (Test-Path $shim) "Shim written to $shim"

        if (Test-Path $shim) {
            $shimText = Get-Content $shim -Raw
            $releaseRoot = Join-Path $binDir "..\releases" | Resolve-Path -ErrorAction SilentlyContinue
            if ($releaseRoot) {
                $releaseRootStr = $releaseRoot.Path
                Assert-True ($shimText -match [regex]::Escape($releaseRootStr)) "Shim points into per-user releases dir ($releaseRootStr), not temp"
            }
            Assert-True ($shimText -notmatch [regex]::Escape($tmpDir)) "Shim does NOT point into APM_TEMP_DIR ($tmpDir)"

            $versionOutput = & cmd.exe /c "`"$shim`" --version" 2>&1
            $versionExit = $LASTEXITCODE
            Assert-True ($versionExit -eq 0) "apm.cmd --version exits 0 (got $versionExit; output: $versionOutput)"
            Assert-True (($versionOutput | Out-String) -match $PinnedVersion.TrimStart("v")) "apm.cmd --version reports $PinnedVersion"
        }

        # The temp dir should be cleaned up by install.ps1's finally block.
        $leftover = Get-ChildItem -Path $tmpDir -Filter "apm-install-*" -Directory -ErrorAction SilentlyContinue
        Assert-True (-not $leftover) "No leftover apm-install-* directory in APM_TEMP_DIR"
    } finally {
        if ($null -ne $savedVersion)      { $env:VERSION = $savedVersion }      else { Remove-Item Env:VERSION -ErrorAction SilentlyContinue }
        if ($null -ne $savedInstallDir)   { $env:APM_INSTALL_DIR = $savedInstallDir } else { Remove-Item Env:APM_INSTALL_DIR -ErrorAction SilentlyContinue }
        if ($null -ne $savedTempDir)      { $env:APM_TEMP_DIR = $savedTempDir } else { Remove-Item Env:APM_TEMP_DIR -ErrorAction SilentlyContinue }
        if ($null -ne $savedSkipChecksum) { $env:APM_SKIP_CHECKSUM = $savedSkipChecksum } else { Remove-Item Env:APM_SKIP_CHECKSUM -ErrorAction SilentlyContinue }

        Remove-Item -Recurse -Force $testRoot -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "=================================================================" -ForegroundColor Blue
Write-Host "        APM install.ps1 Windows integration test                  " -ForegroundColor Blue
Write-Host "=================================================================" -ForegroundColor Blue
Write-Host ""

Test-Sha256Fallback
Test-MoveThenTestOrdering
Test-EndToEndInstall

Write-Host ""
Write-Host "=================================================================" -ForegroundColor Blue
if ($Script:Failures.Count -eq 0) {
    Write-Success "All install.ps1 integration tests passed."
    exit 0
} else {
    Write-Fail "$($Script:Failures.Count) check(s) failed:"
    foreach ($f in $Script:Failures) { Write-Host "  - $f" -ForegroundColor Red }
    exit 1
}
