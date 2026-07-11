# Windows-only end-to-end test for install.ps1.
#
# Covers the fixes for issue microsoft/apm#1389:
#   1. SHA256 verification works on hardened hosts where Get-FileHash is not
#      autoloaded (.NET stream fallback via System.Security.Cryptography).
#   2. The binary smoke test runs from the final install root (under
#      %LOCALAPPDATA%\Programs\apm\releases\...), NOT from %TEMP%, so it
#      survives AppLocker / App Control for Business policies that block
#      executable launch from user-writable temp paths.
#   3. The launchers point at the promoted release directory, including a
#      stable apm.exe that bare CreateProcess callers can resolve.
#   4. Upgrading over an existing install exercises the "move releaseDir
#      aside -> promote staging -> delete backup" path with no leftovers.
#   5. The real `apm self-update` command launches install.ps1 successfully
#      end-to-end (download + dispatch + new version reported).
#
# Designed to run on the windows-latest GitHub Actions runner. Performs a
# real install of a pinned APM release into an isolated test prefix and
# leaves the developer's existing apm install untouched.

param(
    [string]$PinnedVersion = "v0.14.0",
    [string]$OlderVersion  = "v0.13.0"
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
        $childScriptPath = [System.IO.Path]::Combine($env:TEMP, [System.IO.Path]::GetRandomFileName() + ".ps1")
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

    # Parse the script via the PowerShell AST so the assertion is robust to
    # whitespace, line wrapping, added parameters, or quoting changes.
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($InstallScript, [ref]$tokens, [ref]$errors)
    Assert-True ((-not $errors) -or ($errors.Count -eq 0)) "install.ps1 parses cleanly"
    if ($errors -and $errors.Count -gt 0) { return }

    # Find every Move-Item invocation that mentions $packageDir and $stagingDir
    # (either as -Path/-Destination args or as positional values) — that's our
    # staging move.
    $moveCalls = $ast.FindAll({
        param($n)
        if ($n -isnot [System.Management.Automation.Language.CommandAst]) { return $false }
        $cmdName = $n.GetCommandName()
        if ($cmdName -ne 'Move-Item') { return $false }
        $text = $n.Extent.Text
        return ($text -match '\$packageDir' -and $text -match '\$stagingDir')
    }, $true)

    # Find the smoke test invocation: any call expression that invokes
    # $stagedExe with --version.
    $smokeCalls = $ast.FindAll({
        param($n)
        if ($n -isnot [System.Management.Automation.Language.CommandAst]) { return $false }
        $text = $n.Extent.Text
        return ($text -match '\$stagedExe' -and $text -match '--version')
    }, $true)

    Assert-True ($moveCalls.Count -ge 1) "AST: found Move-Item staging call referencing \$packageDir + \$stagingDir"
    Assert-True ($smokeCalls.Count -ge 1) "AST: found smoke-test invocation of \$stagedExe --version"

    if ($moveCalls.Count -ge 1 -and $smokeCalls.Count -ge 1) {
        $firstStageOffset = ($moveCalls | ForEach-Object { $_.Extent.StartOffset } | Sort-Object | Select-Object -First 1)
        $firstSmokeOffset = ($smokeCalls | ForEach-Object { $_.Extent.StartOffset } | Sort-Object | Select-Object -First 1)
        Assert-True ($firstStageOffset -lt $firstSmokeOffset) "Binary smoke test runs AFTER bundle is moved out of temp ($firstStageOffset < $firstSmokeOffset)"
    }
}

# ---------------------------------------------------------------------------
# Test 2b: Test-AntivirusBlockError detects Windows Defender / AV signatures.
# Issue: Defender flags the unsigned PyInstaller binary as PUA (HRESULT
# 0x800700E1), and the installer must distinguish that from AppLocker /
# WDAC denial so it can emit the right guidance (exclusion + pip fallback,
# not allow-list rule).
# ---------------------------------------------------------------------------

function Test-AntivirusDetector {
    Write-Step "Test 2b: Test-AntivirusBlockError matches Defender PUA signatures"

    $content = Get-Content $InstallScript -Raw
    $pattern = '(?s)function Test-AntivirusBlockError\s*\{.*?\n\}'
    $match = [regex]::Match($content, $pattern)
    Assert-True $match.Success "Extracted Test-AntivirusBlockError from install.ps1"
    if (-not $match.Success) { return }

    $accessPattern = '(?s)function Test-AccessDeniedError\s*\{.*?\n\}'
    $accessMatch = [regex]::Match($content, $accessPattern)
    Assert-True $accessMatch.Success "Extracted Test-AccessDeniedError from install.ps1"
    if (-not $accessMatch.Success) { return }

    $childScript = @"
`$ErrorActionPreference = 'Stop'
$($match.Value)
$($accessMatch.Value)

# Real Defender failure text from issue #1389 follow-up:
`$defenderMsg = "Program 'apm.exe' failed to run: Operation did not complete successfully because the file contains a virus or potentially unwanted softwareAt C:\\Users\\X\\AppData\\Local\\Temp\\tmpfoo.ps1:639 char:23"
`$puaMsg      = "blocked: potentially unwanted software detected"
`$hresultMsg  = "CreateProcess failed with 0x800700E1"
`$deletedMsg  = "Operation failed with 0x800700E2 (file removed by antivirus)"
`$accessMsg   = "Program 'apm.exe' failed to run: Access is denied"
`$gpoMsg      = "This program is blocked by group policy. For more information, contact your system administrator. (0x800704EC)"
`$benignMsg   = "exit code 1 - apm: command not found"

`$results = @{
    defender_match = (Test-AntivirusBlockError -Text `$defenderMsg)
    pua_match      = (Test-AntivirusBlockError -Text `$puaMsg)
    hresult_match  = (Test-AntivirusBlockError -Text `$hresultMsg)
    deleted_match  = (Test-AntivirusBlockError -Text `$deletedMsg)
    access_no_av   = (-not (Test-AntivirusBlockError -Text `$accessMsg))
    gpo_no_av      = (-not (Test-AntivirusBlockError -Text `$gpoMsg))
    benign_no_av   = (-not (Test-AntivirusBlockError -Text `$benignMsg))
    empty_no_av    = (-not (Test-AntivirusBlockError -Text ''))
    # Cross-class: AppLocker/SRP/GPO must NOT be misclassified as AV.
    defender_not_access = (-not (Test-AccessDeniedError -Text `$defenderMsg))
    # GPO/SRP block (0x800704EC) belongs in the AppControl bucket, not AV.
    gpo_is_access  = (Test-AccessDeniedError -Text `$gpoMsg)
    access_is_access = (Test-AccessDeniedError -Text `$accessMsg)
}
Write-Output '---APM-JSON-BEGIN---'
Write-Output (`$results | ConvertTo-Json -Compress)
Write-Output '---APM-JSON-END---'
"@

    # Use GetRandomFileName so we don't leak the GetTempFileName-created
    # zero-byte .tmp companion file every run.
    $tempScript = [System.IO.Path]::Combine($env:TEMP, [System.IO.Path]::GetRandomFileName() + ".ps1")
    try {
        Set-Content -Path $tempScript -Value $childScript -Encoding UTF8
        $raw = & pwsh -NoProfile -NonInteractive -File $tempScript 2>&1
        $lines = ($raw | Out-String) -split "`r?`n"
        $begin = [Array]::IndexOf($lines, '---APM-JSON-BEGIN---')
        $end   = [Array]::IndexOf($lines, '---APM-JSON-END---')
        Assert-True (($begin -ge 0) -and ($end -gt $begin)) "Located JSON sentinels in child output"
        if (($begin -lt 0) -or ($end -le $begin)) { return }
        $json = ($lines[($begin + 1)..($end - 1)] -join '').Trim()
        $r = $json | ConvertFrom-Json
        Assert-True ([bool]$r.defender_match)      "Matches real Defender 'contains a virus' message"
        Assert-True ([bool]$r.pua_match)           "Matches 'potentially unwanted software' message"
        Assert-True ([bool]$r.hresult_match)       "Matches HRESULT 0x800700E1 (ERROR_VIRUS_INFECTED)"
        Assert-True ([bool]$r.deleted_match)       "Matches HRESULT 0x800700E2 (ERROR_VIRUS_DELETED)"
        Assert-True ([bool]$r.access_no_av)        "Does not misclassify 'Access is denied' as AV block"
        Assert-True ([bool]$r.gpo_no_av)           "Does not misclassify GPO/SRP block (0x800704EC) as AV"
        Assert-True ([bool]$r.benign_no_av)        "Does not misclassify benign failure text as AV block"
        Assert-True ([bool]$r.empty_no_av)         "Does not match empty input"
        Assert-True ([bool]$r.defender_not_access) "Defender message is not classified as AppLocker/WDAC"
        Assert-True ([bool]$r.gpo_is_access)       "GPO/SRP block (0x800704EC) routes to AppControl guidance"
        Assert-True ([bool]$r.access_is_access)    "'Access is denied' still routes to AppControl guidance"
    } finally {
        Remove-Item -Path $tempScript -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Test 3: Run install.ps1 end-to-end into an isolated prefix.
# ---------------------------------------------------------------------------

function Invoke-InstallScript {
    param(
        [Parameter(Mandatory=$true)][string]$Version,
        [Parameter(Mandatory=$true)][string]$BinDir,
        [Parameter(Mandatory=$true)][string]$TmpDir
    )

    $savedVersion       = $env:VERSION
    $savedInstallDir    = $env:APM_INSTALL_DIR
    $savedTempDir       = $env:APM_TEMP_DIR
    $savedSkipChecksum  = $env:APM_SKIP_CHECKSUM

    try {
        $env:VERSION         = $Version
        $env:APM_INSTALL_DIR = $BinDir
        $env:APM_TEMP_DIR    = $TmpDir
        Remove-Item Env:APM_SKIP_CHECKSUM -ErrorAction SilentlyContinue

        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $InstallScript | Out-Host
        return $LASTEXITCODE
    } finally {
        if ($null -ne $savedVersion)      { $env:VERSION = $savedVersion }            else { Remove-Item Env:VERSION -ErrorAction SilentlyContinue }
        if ($null -ne $savedInstallDir)   { $env:APM_INSTALL_DIR = $savedInstallDir } else { Remove-Item Env:APM_INSTALL_DIR -ErrorAction SilentlyContinue }
        if ($null -ne $savedTempDir)      { $env:APM_TEMP_DIR = $savedTempDir }       else { Remove-Item Env:APM_TEMP_DIR -ErrorAction SilentlyContinue }
        if ($null -ne $savedSkipChecksum) { $env:APM_SKIP_CHECKSUM = $savedSkipChecksum } else { Remove-Item Env:APM_SKIP_CHECKSUM -ErrorAction SilentlyContinue }
    }
}

function Get-ShimVersion {
    param([string]$ShimPath)
    $out = & cmd.exe /c "`"$ShimPath`" --version" 2>&1
    return @{ ExitCode = $LASTEXITCODE; Output = ($out | Out-String).Trim() }
}

function New-IsolatedPrefix {
    # Spaces and a cmd metacharacter exercise launcher/path quoting end to end.
    $root = Join-Path ([System.IO.Path]::GetTempPath()) ("APM Install Test & Edge " + [System.Guid]::NewGuid().ToString("N"))
    $binDir = Join-Path $root "bin"
    $tmpDir = Join-Path $root "tmp"
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    return @{ Root = $root; BinDir = $binDir; TmpDir = $tmpDir }
}

# ---------------------------------------------------------------------------
# Test 3: End-to-end install into an isolated prefix (fresh install path).
# ---------------------------------------------------------------------------

function Test-EndToEndInstall {
    Write-Step "Test 3: End-to-end install of APM $PinnedVersion into isolated prefix"

    $prefix = New-IsolatedPrefix
    try {
        Assert-True ($prefix.Root -match [regex]::Escape(" & ")) "Test prefix exercises spaces and a cmd metacharacter"
        Write-Info "Running install.ps1 (VERSION=$PinnedVersion, APM_INSTALL_DIR=$($prefix.BinDir), APM_TEMP_DIR=$($prefix.TmpDir))"
        $exitCode = Invoke-InstallScript -Version $PinnedVersion -BinDir $prefix.BinDir -TmpDir $prefix.TmpDir
        Assert-True ($exitCode -eq 0) "install.ps1 exits 0 (got $exitCode)"

        $shim = Join-Path $prefix.BinDir "apm.cmd"
        Assert-True (Test-Path $shim) "Shim written to $shim"

        if (Test-Path $shim) {
            $shimText = Get-Content $shim -Raw
            $releaseRoot = Join-Path $prefix.BinDir "..\releases" | Resolve-Path -ErrorAction SilentlyContinue
            if ($releaseRoot) {
                # When the release directory lives under %LOCALAPPDATA% (the default
                # install root, and also true of the agent runner's %TEMP%), the shim
                # embeds the literal %LOCALAPPDATA% token (issue microsoft/apm#1509)
                # instead of the expanded profile path. Accept either form here so
                # the assertion verifies "shim points at the install location" rather
                # than the on-disk encoding strategy.
                $expectedExpanded = $releaseRoot.Path
                $localAppData = $env:LOCALAPPDATA
                $expectedTokenized = $null
                if ($localAppData -and $expectedExpanded.StartsWith($localAppData, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $expectedTokenized = "%LOCALAPPDATA%" + $expectedExpanded.Substring($localAppData.Length)
                }
                $matchesExpanded  = $shimText -match [regex]::Escape($expectedExpanded)
                $matchesTokenized = $expectedTokenized -and ($shimText -match [regex]::Escape($expectedTokenized))
                Assert-True ($matchesExpanded -or $matchesTokenized) "Shim points into per-user releases dir ($expectedExpanded), not temp"
            }
            Assert-True ($shimText -notmatch [regex]::Escape($prefix.TmpDir)) "Shim does NOT point into APM_TEMP_DIR ($($prefix.TmpDir))"

            $ver = Get-ShimVersion -ShimPath $shim
            Assert-True ($ver.ExitCode -eq 0) "apm.cmd --version exits 0 (got $($ver.ExitCode); output: $($ver.Output))"
            Assert-True ($ver.Output -match $PinnedVersion.TrimStart("v")) "apm.cmd --version reports $PinnedVersion"
        }

        $currentDir = Join-Path $prefix.Root "current"
        $stableExe = Join-Path $currentDir "apm.exe"
        Assert-True (Test-Path $stableExe) "Stable apm.exe is available at $stableExe"

        if (Test-Path $stableExe) {
            $savedPath = $env:Path
            try {
                $env:Path = "$currentDir;$env:Path"
                $pythonScript = @'
import os
import subprocess
import sys

result = subprocess.run(
    ["apm", "--version"],
    capture_output=True,
    cwd=os.environ["APM_LAUNCH_TEST_CWD"],
    text=True,
)
print(result.stdout, end="")
print(result.stderr, end="", file=sys.stderr)
sys.exit(result.returncode)
'@
                $env:APM_LAUNCH_TEST_CWD = $prefix.Root
                $pythonOutput = & python -c $pythonScript 2>&1
                $pythonExit = $LASTEXITCODE
                Assert-True ($pythonExit -eq 0) "Python subprocess resolves bare apm (got $pythonExit; output: $pythonOutput)"
                Assert-True (($pythonOutput | Out-String) -match [regex]::Escape($PinnedVersion.TrimStart("v"))) "Python subprocess reports $PinnedVersion"

                $bash = Get-Command bash -ErrorAction SilentlyContinue
                if ($bash) {
                    $bashCurrentDir = (& $bash.Source -lc 'cygpath -u "$1"' bash $currentDir).Trim()
                    $bashTestDir = (& $bash.Source -lc 'cygpath -u "$1"' bash $prefix.Root).Trim()
                    $bashOutput = & $bash.Source -lc 'cd "$1" && PATH="$2:$PATH" && command -v apm && apm --version' bash $bashTestDir $bashCurrentDir 2>&1
                    $bashExit = $LASTEXITCODE
                    Assert-True ($bashExit -eq 0) "Git Bash resolves bare apm (got $bashExit; output: $bashOutput)"
                    Assert-True (($bashOutput | Out-String) -match [regex]::Escape($PinnedVersion.TrimStart("v"))) "Git Bash reports $PinnedVersion"
                }
            } finally {
                Remove-Item Env:APM_LAUNCH_TEST_CWD -ErrorAction SilentlyContinue
                $env:Path = $savedPath
            }
        }

        $leftover = Get-ChildItem -Path $prefix.TmpDir -Filter "apm-install-*" -Directory -ErrorAction SilentlyContinue
        Assert-True (-not $leftover) "No leftover apm-install-* directory in APM_TEMP_DIR"
    } finally {
        Remove-Item -Recurse -Force $prefix.Root -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Test 4a: Cross-version upgrade. Install OlderVersion, then PinnedVersion,
# into the same prefix. The shim must end up pointing at PinnedVersion's
# release dir and `apm --version` must report PinnedVersion.
# ---------------------------------------------------------------------------

function Test-CrossVersionUpgrade {
    Write-Step "Test 4a: Cross-version upgrade $OlderVersion -> $PinnedVersion in same prefix"

    $prefix = New-IsolatedPrefix
    try {
        Write-Info "Step 1: install $OlderVersion"
        $exit1 = Invoke-InstallScript -Version $OlderVersion -BinDir $prefix.BinDir -TmpDir $prefix.TmpDir
        Assert-True ($exit1 -eq 0) "Step 1 install.ps1 exits 0 (got $exit1)"

        $shim = Join-Path $prefix.BinDir "apm.cmd"
        $ver1 = Get-ShimVersion -ShimPath $shim
        Assert-True ($ver1.Output -match $OlderVersion.TrimStart("v")) "Step 1: apm.cmd --version reports $OlderVersion (got: $($ver1.Output))"

        Write-Info "Step 2: install $PinnedVersion over the existing install"
        $exit2 = Invoke-InstallScript -Version $PinnedVersion -BinDir $prefix.BinDir -TmpDir $prefix.TmpDir
        Assert-True ($exit2 -eq 0) "Step 2 install.ps1 exits 0 (got $exit2)"

        $ver2 = Get-ShimVersion -ShimPath $shim
        Assert-True ($ver2.Output -match $PinnedVersion.TrimStart("v")) "Step 2: apm.cmd --version reports $PinnedVersion (got: $($ver2.Output))"

        $shimText = Get-Content $shim -Raw
        Assert-True ($shimText -match [regex]::Escape($PinnedVersion)) "Step 2: shim references $PinnedVersion path"

        # The version-stable junction must re-point at the new release so bare
        # CreateProcess / Git Bash callers resolve the upgraded apm.exe, not the
        # previous release still on disk. A wrong-target junction survives the
        # installer's own Test-Path guard, so assert the resolved version too.
        $stableExe = Join-Path $prefix.Root "current\apm.exe"
        Assert-True (Test-Path $stableExe) "Stable current\apm.exe exists after upgrade"
        if (Test-Path $stableExe) {
            $stableVer = & $stableExe --version 2>&1
            Assert-True (($stableVer | Out-String) -match [regex]::Escape($PinnedVersion.TrimStart("v"))) "current\apm.exe reports $PinnedVersion after upgrade"
        }
        $junctionLeftover = Get-ChildItem -Path $prefix.Root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "current.new-*" -or $_.Name -like "current.old-*" }
        Assert-True (-not $junctionLeftover) "No leftover current.new-* / current.old-* junction temps at install root"

        # Both release dirs may coexist (we only replace the matching tag),
        # but the staging/backup helper dirs from the second install MUST be
        # cleaned up.
        $releasesDir = Join-Path $prefix.BinDir "..\releases" | Resolve-Path
        $leftoverStaging = Get-ChildItem -Path $releasesDir -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "*.new-*" -or $_.Name -like "*.old-*" }
        Assert-True (-not $leftoverStaging) "No leftover .new-* / .old-* staging/backup dirs after upgrade"
    } finally {
        Remove-Item -Recurse -Force $prefix.Root -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Test 4b: Same-version reinstall. This is the path that exercises the
# stage -> move-existing-aside -> promote -> delete-backup branch in
# install.ps1, because $releaseDir already exists for the same tag.
# ---------------------------------------------------------------------------

function Test-SameVersionReinstall {
    Write-Step "Test 4b: Same-version reinstall of $PinnedVersion exercises promote/backup branch"

    $prefix = New-IsolatedPrefix
    try {
        Write-Info "Step 1: install $PinnedVersion"
        $exit1 = Invoke-InstallScript -Version $PinnedVersion -BinDir $prefix.BinDir -TmpDir $prefix.TmpDir
        Assert-True ($exit1 -eq 0) "Step 1 install.ps1 exits 0 (got $exit1)"

        $releasesDir = Join-Path $prefix.BinDir "..\releases" | Resolve-Path
        $releaseDir = Join-Path $releasesDir $PinnedVersion
        Assert-True (Test-Path $releaseDir) "Release dir exists after first install ($releaseDir)"
        $firstExe = Join-Path $releaseDir "apm.exe"
        $firstStamp = (Get-Item $firstExe).LastWriteTimeUtc

        Write-Info "Step 2: reinstall $PinnedVersion (must rename releaseDir aside, promote staging, delete backup)"
        $exit2 = Invoke-InstallScript -Version $PinnedVersion -BinDir $prefix.BinDir -TmpDir $prefix.TmpDir
        Assert-True ($exit2 -eq 0) "Step 2 install.ps1 exits 0 (got $exit2)"

        Assert-True (Test-Path $releaseDir) "Release dir still exists after reinstall"
        $secondExe = Join-Path $releaseDir "apm.exe"
        Assert-True (Test-Path $secondExe) "apm.exe present after reinstall"

        # apm.exe must be the freshly staged copy, not the original (the
        # promote step renames the old release dir aside and moves the
        # staging dir into place, so write time must be >= first stamp).
        $secondStamp = (Get-Item $secondExe).LastWriteTimeUtc
        Assert-True ($secondStamp -ge $firstStamp) "apm.exe write time advanced after reinstall ($firstStamp -> $secondStamp)"

        $ver = Get-ShimVersion -ShimPath (Join-Path $prefix.BinDir "apm.cmd")
        Assert-True ($ver.ExitCode -eq 0) "apm.cmd --version exits 0 after reinstall (got $($ver.ExitCode))"
        Assert-True ($ver.Output -match $PinnedVersion.TrimStart("v")) "apm.cmd --version reports $PinnedVersion after reinstall"

        # The junction is rebuilt on every install; after a same-version
        # reinstall it must still resolve the freshly promoted apm.exe for bare
        # CreateProcess / Git Bash callers.
        $stableExe = Join-Path $prefix.Root "current\apm.exe"
        Assert-True (Test-Path $stableExe) "Stable current\apm.exe exists after reinstall"
        if (Test-Path $stableExe) {
            $stableVer = & $stableExe --version 2>&1
            Assert-True (($stableVer | Out-String) -match [regex]::Escape($PinnedVersion.TrimStart("v"))) "current\apm.exe reports $PinnedVersion after reinstall"
        }
        $junctionLeftover = Get-ChildItem -Path $prefix.Root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "current.new-*" -or $_.Name -like "current.old-*" }
        Assert-True (-not $junctionLeftover) "No leftover current.new-* / current.old-* junction temps at install root after reinstall"

        $leftoverStaging = Get-ChildItem -Path $releasesDir -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "*.new-*" -or $_.Name -like "*.old-*" }
        Assert-True (-not $leftoverStaging) "No leftover .new-* / .old-* dirs after reinstall (rollback path didn't trigger and backup was deleted)"
    } finally {
        Remove-Item -Recurse -Force $prefix.Root -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Test 5: Real `apm self-update` end-to-end. Install OlderVersion, then run
# the installed apm.cmd's self-update command. The installed apm downloads
# install.ps1 from aka.ms/apm-windows and runs it, exercising the whole
# launch path that issue #1389 originally broke. The fresh apm.cmd must
# report a version >= PinnedVersion afterwards.
#
# Caveat: self-update fetches the install.ps1 currently published at
# aka.ms/apm-windows (main branch), NOT the one in this PR. So this test
# proves the *launch path* and *upgrade flow* work end-to-end on a clean
# Windows runner. The new fixes in this PR are validated by Tests 1-4.
# ---------------------------------------------------------------------------

function Test-SelfUpdateCommand {
    Write-Step "Test 5: apm self-update end-to-end (start at $OlderVersion, expect upgrade)"

    $prefix = New-IsolatedPrefix
    try {
        Write-Info "Step 1: install $OlderVersion as the starting binary"
        $exit1 = Invoke-InstallScript -Version $OlderVersion -BinDir $prefix.BinDir -TmpDir $prefix.TmpDir
        Assert-True ($exit1 -eq 0) "Step 1 install.ps1 exits 0 (got $exit1)"

        $shim = Join-Path $prefix.BinDir "apm.cmd"
        $ver1 = Get-ShimVersion -ShimPath $shim
        Assert-True ($ver1.Output -match $OlderVersion.TrimStart("v")) "Step 1: apm.cmd --version reports $OlderVersion (got: $($ver1.Output))"

        Write-Info "Step 2: run apm self-update (downloads + dispatches install.ps1 from aka.ms/apm-windows)"
        # Point the self-update temp file at our isolated prefix so we don't
        # litter the runner's %LOCALAPPDATA% and so the staged install.ps1
        # has a writable temp dir.
        # Install.ps1 honours APM_INSTALL_DIR and APM_TEMP_DIR; setting both
        # here ensures the downloaded installer reuses our isolated prefix
        # instead of writing to %LOCALAPPDATA%\Programs\apm. self-update's
        # subprocess inherits these via os.environ -> external_process_env().
        $savedTempDir    = $env:APM_TEMP_DIR
        $savedInstallDir = $env:APM_INSTALL_DIR
        $env:APM_TEMP_DIR    = $prefix.TmpDir
        $env:APM_INSTALL_DIR = $prefix.BinDir
        try {
            $output = & cmd.exe /c "`"$shim`" self-update" 2>&1
            $selfUpdateExit = $LASTEXITCODE
        } finally {
            if ($null -ne $savedTempDir)    { $env:APM_TEMP_DIR = $savedTempDir }       else { Remove-Item Env:APM_TEMP_DIR -ErrorAction SilentlyContinue }
            if ($null -ne $savedInstallDir) { $env:APM_INSTALL_DIR = $savedInstallDir } else { Remove-Item Env:APM_INSTALL_DIR -ErrorAction SilentlyContinue }
        }

        Write-Info "self-update output (last 20 lines):"
        ($output | Out-String).Split("`n") | Select-Object -Last 20 | ForEach-Object { Write-Host "    $_" }

        Assert-True ($selfUpdateExit -eq 0) "apm self-update exits 0 (got $selfUpdateExit)"

        $ver2 = Get-ShimVersion -ShimPath $shim
        Assert-True ($ver2.ExitCode -eq 0) "apm.cmd --version exits 0 after self-update"

        # After self-update, version must have advanced past OlderVersion.
        # We can't pin to PinnedVersion exactly because aka.ms/apm-windows
        # always grabs the current latest, which may move ahead of this PR.
        $oldNumeric = $OlderVersion.TrimStart("v")
        Assert-True ($ver2.Output -notmatch [regex]::Escape($oldNumeric)) "apm.cmd --version no longer reports $OlderVersion after self-update (got: $($ver2.Output))"
    } finally {
        Remove-Item -Recurse -Force $prefix.Root -ErrorAction SilentlyContinue
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
Test-AntivirusDetector
Test-EndToEndInstall
Test-CrossVersionUpgrade
Test-SameVersionReinstall
Test-SelfUpdateCommand

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
