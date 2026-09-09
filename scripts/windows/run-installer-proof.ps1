# Focused Windows installer proof for one locally built candidate archive.
#
# The proof runs the unchanged installer assertion harness twice against the
# same archive with explicit pytest basetemps: one long root shaped like the
# failing default runner root, and one short root under RUNNER_TEMP. It also
# records archive path pressure and PowerShell 5.1 native-stderr behavior.

param(
    [string]$CandidateArchive,
    [string]$CandidateMetadata,
    [string]$BaselineArchive,
    [string]$BaselineChecksum,
    [string]$BaselineVersion,
    [string]$OutputDir = "windows-installer-proof",
    [switch]$SelfTestProcessCapture,
    [string]$ValidateInstallerJUnit,
    [int]$InstallerExitCode = 0,
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"

function Resolve-RequiredPath {
    param([string]$Path, [string]$Label)
    if (-not $Path) { throw "$Label is required" }
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    if (-not $resolved) { throw "$Label not found: $Path" }
    return $resolved.Path
}

function Get-Sha256Hex {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-JsonFile {
    param([object]$Value, [string]$Path)
    $directory = Split-Path -Parent $Path
    if ($directory) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
    $Value | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Stop-OwnedProcessTree {
    param([System.Diagnostics.Process]$Process)
    if ($Process.HasExited) { return }
    if ($IsWindows) {
        & taskkill.exe /PID $Process.Id /T /F | Out-Null
        if ($LASTEXITCODE -ne 0 -and -not $Process.HasExited) {
            throw "Could not stop owned process tree $($Process.Id)"
        }
    } else {
        $Process.Kill($true)
    }
}

function Invoke-CapturedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$TimeoutSeconds = 90,
        [hashtable]$Environment = @{},
        [string]$WorkingDirectory = (Get-Location).Path
    )
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    foreach ($argument in $Arguments) {
        [void]$psi.ArgumentList.Add($argument)
    }
    foreach ($key in $Environment.Keys) {
        $psi.Environment[$key] = [string]$Environment[$key]
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $psi
    $record = [ordered]@{
        name = $Name
        command = @($FilePath) + $Arguments
        cwd = $WorkingDirectory
        timeout_seconds = $TimeoutSeconds
        timed_out = $false
        exit_code = $null
        stdout = ""
        stderr = ""
    }
    try {
        if (-not $process.Start()) { throw "Could not start $Name" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $record.timed_out = $true
            Stop-OwnedProcessTree -Process $process
            if (-not $process.WaitForExit(10000)) {
                throw "Owned process $($process.Id) did not exit after its timeout"
            }
        }
        $record.exit_code = $process.ExitCode
        $record.stdout = $stdoutTask.GetAwaiter().GetResult()
        $record.stderr = $stderrTask.GetAwaiter().GetResult()
        return $record
    } finally {
        $process.Dispose()
    }
}

function New-OwnedBaseTemp {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        throw "Refusing to reuse preexisting proof basetemp: $Path"
    }
    New-Item -ItemType Directory -Path $Path -ErrorAction Stop | Out-Null
    Set-Content -LiteralPath (Join-Path $Path ".apm-windows-installer-proof-owned") `
        -Value "owned by run $env:GITHUB_RUN_ID attempt $env:GITHUB_RUN_ATTEMPT" -Encoding ASCII
    return $Path
}

function Remove-OwnedBaseTemp {
    param([string]$Path)
    $marker = Join-Path $Path ".apm-windows-installer-proof-owned"
    if (-not (Test-Path -LiteralPath $marker)) {
        throw "Refusing to remove unmarked proof basetemp: $Path"
    }
    Remove-Item -Recurse -Force -LiteralPath $Path -ErrorAction Stop
}

function Read-BaselineSha {
    param([string]$Path)
    $text = (Get-Content -LiteralPath $Path -Raw).Trim()
    if ($text -notmatch '^([a-fA-F0-9]{64})\s+\*?apm-windows-x86_64\.zip$') {
        throw "Invalid baseline checksum format: $Path"
    }
    return $Matches[1].ToLowerInvariant()
}

function Get-ArchiveInventory {
    param(
        [string]$Archive,
        [string]$LongBaseTemp,
        [string]$ShortBaseTemp,
        [string]$Version
    )
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $allEntries = @($zip.Entries)
        $files = @($allEntries | Where-Object { -not ($_.FullName.EndsWith("/")) })
        $entries = @($files | ForEach-Object { $_.FullName })
    } finally {
        $zip.Dispose()
    }
    $suffix = "APM Install Test & Edge " + ("a" * 32)
    function Convert-MemberPath {
        param([string]$Member)
        $prefix = "apm-windows-x86_64/"
        if ($Member.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
            return $Member.Substring($prefix.Length).Replace("/", "\")
        }
        return $Member.Replace("/", "\")
    }
    function Measure-BaseTemp {
        param([string]$BaseTemp)
        $testRoot = Join-Path $BaseTemp "wi0\i\work"
        $installRoot = Join-Path $testRoot $suffix
        $tempDir = Join-Path (Join-Path $installRoot "tmp") ("apm-install-" + ("b" * 32))
        $stageDir = Join-Path (Join-Path $installRoot "releases") ($Version + ".new-" + ("c" * 32))
        $finalDir = Join-Path (Join-Path $installRoot "releases") $Version
        $records = @()
        foreach ($entry in $entries) {
            $relative = Convert-MemberPath -Member $entry
            $records += [ordered]@{
                member = $entry
                temp_length = (Join-Path $tempDir ($entry.Replace("/", "\"))).Length
                stage_length = (Join-Path $stageDir $relative).Length
                final_length = (Join-Path $finalDir $relative).Length
            }
        }
        return [ordered]@{
            base_temp = $BaseTemp
            test_root = $testRoot
            install_root = $installRoot
            max_temp = @($records | Sort-Object temp_length -Descending | Select-Object -First 1)[0]
            max_stage = @($records | Sort-Object stage_length -Descending | Select-Object -First 1)[0]
            max_final = @($records | Sort-Object final_length -Descending | Select-Object -First 1)[0]
            charset = @($records | Where-Object { $_.member -match 'charset_normalizer|chardet' })
            stage_over_260 = @($records | Where-Object { $_.stage_length -ge 260 }).Count
            temp_over_260 = @($records | Where-Object { $_.temp_length -ge 260 }).Count
        }
    }
    return [ordered]@{
        file_count = $files.Count
        member_count = $allEntries.Count
        pyd_count = @($entries | Where-Object { ($_.ToLowerInvariant()).EndsWith(".pyd") }).Count
        long_root = Measure-BaseTemp -BaseTemp $LongBaseTemp
        short_root = Measure-BaseTemp -BaseTemp $ShortBaseTemp
    }
}

function Invoke-DirectCandidateProbe {
    param(
        [string]$Name,
        [string]$BaseTemp,
        [string]$Archive,
        [string]$Version,
        [string]$OutDir
    )
    $testRoot = Join-Path $BaseTemp "wi0\i\work"
    $root = Join-Path $testRoot ("APM Install Test & Edge " + [System.Guid]::NewGuid().ToString("N"))
    $extractDir = Join-Path $root ("tmp\apm-install-" + [System.Guid]::NewGuid().ToString("N"))
    $releaseDir = Join-Path (Join-Path $root "releases") $Version
    $stagingDir = "$releaseDir.new-" + [System.Guid]::NewGuid().ToString("N")
    $record = [ordered]@{
        name = $Name
        powershell_host = [ordered]@{
            edition = $PSVersionTable.PSEdition
            version = $PSVersionTable.PSVersion.ToString()
        }
        base_temp = $BaseTemp
        test_root = $testRoot
        root = $root
        extract_dir = $extractDir
        staging_dir = $stagingDir
        expanded = $false
        moved_to_stage = $false
        charset_members = @()
        run = $null
        error = $null
    }
    try {
        New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $releaseDir) | Out-Null
        Expand-Archive -LiteralPath $Archive -DestinationPath $extractDir -Force
        $record.expanded = $true
        $packageDir = Join-Path $extractDir "apm-windows-x86_64"
        Get-ChildItem -LiteralPath $packageDir -Recurse -Force -ErrorAction Stop |
            Where-Object { $_.FullName -match 'charset_normalizer|chardet' } |
            ForEach-Object {
                $record.charset_members += [ordered]@{
                    path = $_.FullName
                    length = $_.FullName.Length
                    exists = $_.Exists
                }
            }
        Move-Item -LiteralPath $packageDir -Destination $stagingDir -Force
        $record.moved_to_stage = $true
        $exe = Join-Path $stagingDir "apm.exe"
        $record.run = Invoke-CapturedProcess -FilePath $exe -Arguments @("--version") `
            -Name "$Name direct apm --version" -TimeoutSeconds 90
    } catch {
        $record.error = "$_"
    } finally {
        Write-JsonFile -Value $record -Path (Join-Path $OutDir "direct-$Name.json")
    }
    return $record
}

function Invoke-PowerShellNativeStderrProbe {
    param([string]$OutDir)
    $child = Join-Path $OutDir "ps5-native-stderr-child.ps1"
    @'
$ErrorActionPreference = "Stop"
function Invoke-NativeCase {
    param([string]$Name, [int]$ExitCode)
    $result = [ordered]@{
        name = $Name
        expected_exit_code = $ExitCode
        threw = $false
        last_exit_code = $null
        output = ""
        error = ""
    }
    try {
        $output = & cmd.exe /d /c "echo marker-$Name 1>&2 & exit /b $ExitCode" 2>&1
        $result.last_exit_code = $LASTEXITCODE
        $result.output = ($output | Out-String)
    } catch {
        $result.threw = $true
        $result.last_exit_code = $LASTEXITCODE
        $result.error = "$_"
    }
    return $result
}
[ordered]@{
    host = [ordered]@{
        edition = $PSVersionTable.PSEdition
        version = $PSVersionTable.PSVersion.ToString()
        major = $PSVersionTable.PSVersion.Major
        minor = $PSVersionTable.PSVersion.Minor
    }
    cases = @(
        (Invoke-NativeCase -Name "exit0-stderr" -ExitCode 0),
        (Invoke-NativeCase -Name "exit23-stderr" -ExitCode 23)
    )
} | ConvertTo-Json -Depth 8
'@ | Set-Content -LiteralPath $child -Encoding UTF8
    $ps5 = Invoke-CapturedProcess -FilePath "powershell.exe" -Arguments @(
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $child
    ) -Name "powershell 5 native stderr probe" -TimeoutSeconds 90
    $exit0 = Invoke-CapturedProcess -FilePath "cmd.exe" -Arguments @(
        "/d", "/c", "echo marker-direct-exit0 1>&2 & exit /b 0"
    ) -Name "direct native stderr exit0 control" -TimeoutSeconds 90
    $exit23 = Invoke-CapturedProcess -FilePath "cmd.exe" -Arguments @(
        "/d", "/c", "echo marker-direct-exit23 1>&2 & exit /b 23"
    ) -Name "direct native stderr exit23 control" -TimeoutSeconds 90
    if ($exit0.timed_out -or $exit0.exit_code -ne 0 -or $exit0.stderr -notmatch "marker-direct-exit0") {
        throw "Broken direct exit0 native-stderr control"
    }
    if ($exit23.timed_out -or $exit23.exit_code -ne 23 -or $exit23.stderr -notmatch "marker-direct-exit23") {
        throw "Broken direct exit23 native-stderr control"
    }
    if ($ps5.timed_out -or $ps5.exit_code -ne 0) {
        throw "PowerShell 5 native-stderr child failed"
    }
    try {
        $ps5Json = $ps5.stdout | ConvertFrom-Json
    } catch {
        throw "PowerShell 5 native-stderr child did not emit valid JSON: $_"
    }
    if ($ps5Json.host.major -ne 5 -or $ps5Json.host.minor -ne 1) {
        throw "Expected Windows PowerShell 5.1, got $($ps5Json.host.version)"
    }
    $caseNames = @($ps5Json.cases | ForEach-Object { $_.name } | Sort-Object)
    if ($caseNames.Count -ne 2 -or ($caseNames -join ",") -ne "exit0-stderr,exit23-stderr") {
        throw "PowerShell 5 child did not report both native-stderr cases"
    }
    foreach ($case in $ps5Json.cases) {
        # A terminating stderr record can prevent LASTEXITCODE from being updated.
        # The independent native controls above establish the real exit codes.
        if (-not $case.threw -and $case.name -eq "exit0-stderr" -and $case.last_exit_code -ne 0) {
            throw "PowerShell 5 exit0 case lost native exit code"
        }
        if (-not $case.threw -and $case.name -eq "exit23-stderr" -and $case.last_exit_code -ne 23) {
            throw "PowerShell 5 exit23 case lost native exit code"
        }
        $combined = "$($case.output) $($case.error)"
        if ($combined -notmatch ("marker-" + [regex]::Escape($case.name))) {
            throw "PowerShell 5 case $($case.name) lost stderr marker"
        }
    }
    $result = [ordered]@{
        name = "deterministic-native-stderr"
        powershell5 = $ps5
        powershell5_json = $ps5Json
        direct_exit0 = $exit0
        direct_exit23 = $exit23
    }
    Write-JsonFile -Value $result -Path (Join-Path $OutDir "native-stderr-behavior.json")
    return $result
}

function Read-InstallerOutcome {
    param([string]$JUnitPath, [int]$ExitCode)
    [xml]$report = Get-Content -LiteralPath $JUnitPath -Raw -ErrorAction Stop
    $cases = @($report.SelectNodes("//testcase"))
    $skipped = @($report.SelectNodes("//testcase/skipped")).Count
    $failed = @($report.SelectNodes("//testcase/failure | //testcase/error")).Count
    if ($cases.Count -eq 0 -or $skipped -ne 0) {
        throw "Installer assertions did not execute completely: $($cases.Count) cases, $skipped skipped"
    }
    if ($ExitCode -eq 0 -and $failed -ne 0) {
        throw "Installer exit code claimed success despite $failed failing JUnit cases"
    }
    return [ordered]@{ cases = $cases.Count; skipped = $skipped; failed = $failed }
}

function Invoke-InstallerPytest {
    param(
        [string]$Name,
        [string]$OutDir,
        [string]$BaseTemp,
        [hashtable]$InstallerEnvironment
    )
    if (Test-Path -LiteralPath $BaseTemp) {
        throw "Installer pytest basetemp must be fresh before pytest owns it: $BaseTemp"
    }
    $arguments = @(
        "run", "--frozen", "--no-sync", "pytest",
        "tests/integration/test_windows_installer_launchers.py",
        "-q", "-s", "--tb=short",
        "--junitxml=$OutDir\junit-$Name.xml",
        "--basetemp", $BaseTemp
    )
    $result = Invoke-CapturedProcess -FilePath "uv" -Arguments $arguments `
        -Name "$Name installer pytest" -TimeoutSeconds 900 -Environment $InstallerEnvironment
    # Persist raw diagnostics even when pytest never produced usable JUnit.
    $result.stdout | Set-Content -LiteralPath (Join-Path $OutDir "$Name-stdout.log") -Encoding UTF8
    $result.stderr | Set-Content -LiteralPath (Join-Path $OutDir "$Name-stderr.log") -Encoding UTF8
    Write-JsonFile -Value $result -Path (Join-Path $OutDir "$Name-result.json")
    Write-Host "[$Name] exit $($result.exit_code) timed_out=$($result.timed_out)"
    if ($result.stdout) {
        ($result.stdout -split "`n") | Select-Object -Last 30 | ForEach-Object { Write-Host "[$Name stdout] $_" }
    }
    if ($result.stderr) {
        ($result.stderr -split "`n") | Select-Object -Last 30 | ForEach-Object { Write-Host "[$Name stderr] $_" }
    }
    try {
        $result.junit = Read-InstallerOutcome -JUnitPath (Join-Path $OutDir "junit-$Name.xml") `
            -ExitCode $result.exit_code
    } catch {
        $result.junit_validation_error = $_.Exception.Message
        Write-JsonFile -Value $result -Path (Join-Path $OutDir "$Name-result.json")
        throw
    }
    Write-JsonFile -Value $result -Path (Join-Path $OutDir "$Name-result.json")
    return $result
}

function Invoke-ProcessCaptureSelfTest {
    param([string]$OutDir, [string]$Python)
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $exit0 = Invoke-CapturedProcess -FilePath $Python -Arguments @(
        "-c", "import sys; sys.stderr.write('selftest-exit0-stderr\n'); print('selftest-exit0-stdout')"
    ) -Name "selftest exit0" -TimeoutSeconds 30
    $exit23 = Invoke-CapturedProcess -FilePath $Python -Arguments @(
        "-c", "import sys; sys.stderr.write('selftest-exit23-stderr\n'); sys.exit(23)"
    ) -Name "selftest exit23" -TimeoutSeconds 30
    $timeout = Invoke-CapturedProcess -FilePath $Python -Arguments @(
        "-c", "import time; time.sleep(10)"
    ) -Name "selftest timeout" -TimeoutSeconds 1
    $result = [ordered]@{ exit0 = $exit0; exit23 = $exit23; timeout = $timeout }
    Write-JsonFile -Value $result -Path (Join-Path $OutDir "process-capture-selftest.json")
    if ($exit0.timed_out -or $exit0.exit_code -ne 0 -or $exit0.stderr -notmatch "selftest-exit0-stderr") {
        throw "Process capture self-test exit0 failed"
    }
    if ($exit23.timed_out -or $exit23.exit_code -ne 23 -or $exit23.stderr -notmatch "selftest-exit23-stderr") {
        throw "Process capture self-test exit23 failed"
    }
    if (-not $timeout.timed_out) {
        throw "Process capture self-test timeout did not time out"
    }
}

if ($ValidateInstallerJUnit) {
    Read-InstallerOutcome -JUnitPath $ValidateInstallerJUnit -ExitCode $InstallerExitCode |
        ConvertTo-Json
    exit 0
}

if ($SelfTestProcessCapture) {
    Invoke-ProcessCaptureSelfTest -OutDir $OutputDir -Python $PythonExecutable
    exit 0
}

$CandidateArchive = Resolve-RequiredPath -Path $CandidateArchive -Label "Candidate archive"
$CandidateMetadata = Resolve-RequiredPath -Path $CandidateMetadata -Label "Candidate metadata"
$BaselineArchive = Resolve-RequiredPath -Path $BaselineArchive -Label "Baseline archive"
$BaselineChecksum = Resolve-RequiredPath -Path $BaselineChecksum -Label "Baseline checksum"
if (-not $BaselineVersion) { throw "BaselineVersion is required" }
if (-not $env:RUNNER_TEMP) { throw "RUNNER_TEMP is required; this proof is CI-only" }
if (-not $env:TEMP) { throw "TEMP is required to reproduce the Windows pytest default root" }
if ([string]$env:GITHUB_RUN_ATTEMPT -ne "1") {
    throw "Use a fresh attempt 1 run for Windows installer proof"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$metadata = Get-Content -LiteralPath $CandidateMetadata -Raw | ConvertFrom-Json
$candidateVersion = "v$($metadata.version)"
$candidateSha = [string]$metadata.archive_sha256
$candidateExeSha = [string]$metadata.executable_sha256
if ((Get-Sha256Hex -Path $CandidateArchive) -cne $candidateSha) {
    throw "Candidate archive hash does not match metadata"
}
$baselineSha = Read-BaselineSha -Path $BaselineChecksum
if ((Get-Sha256Hex -Path $BaselineArchive) -cne $baselineSha) {
    throw "Baseline archive hash does not match sidecar"
}

$resolvedTemp = Invoke-CapturedProcess -FilePath "python" -Arguments @(
    "-c", "import os; print(os.path.realpath(os.environ['TEMP']))"
) -Name "Resolve actual long TEMP path"
if ($resolvedTemp.timed_out -or $resolvedTemp.exit_code -ne 0) {
    throw "Could not resolve the actual Windows TEMP path"
}
$longChild = "wp-" + [System.Guid]::NewGuid().ToString("N").Substring(0, 5)
$longBaseTemp = Join-Path (Join-Path $resolvedTemp.stdout.Trim() ("pytest-of-" + $env:USERNAME)) $longChild
$shortBaseTemp = Join-Path $env:RUNNER_TEMP "apm-windows-installer"
Invoke-PowerShellNativeStderrProbe -OutDir $OutputDir | Out-Null
$longOwned = $false
$shortOwned = $false
try {
    New-OwnedBaseTemp -Path $longBaseTemp | Out-Null
    $longOwned = $true
    New-OwnedBaseTemp -Path $shortBaseTemp | Out-Null
    $shortOwned = $true
    $inventory = Get-ArchiveInventory -Archive $CandidateArchive -LongBaseTemp $longBaseTemp `
        -ShortBaseTemp $shortBaseTemp -Version $candidateVersion
    Write-JsonFile -Value $inventory -Path (Join-Path $OutputDir "archive-path-inventory.json")
    Invoke-DirectCandidateProbe -Name "long-root" -BaseTemp $longBaseTemp -Archive $CandidateArchive `
        -Version $candidateVersion -OutDir $OutputDir | Out-Null
    Invoke-DirectCandidateProbe -Name "short-root" -BaseTemp $shortBaseTemp -Archive $CandidateArchive `
        -Version $candidateVersion -OutDir $OutputDir | Out-Null
} finally {
    if ($longOwned -and (Test-Path -LiteralPath $longBaseTemp)) { Remove-OwnedBaseTemp -Path $longBaseTemp }
    if ($shortOwned -and (Test-Path -LiteralPath $shortBaseTemp)) { Remove-OwnedBaseTemp -Path $shortBaseTemp }
}

$installerEnvironment = @{
    "APM_E2E_TESTS" = "1"
    "APM_CANDIDATE_ARCHIVE" = $CandidateArchive
    "APM_CANDIDATE_VERSION" = $candidateVersion
    "APM_CANDIDATE_SHA256" = $candidateSha
    "APM_BASELINE_ARCHIVE" = $BaselineArchive
    "APM_BASELINE_VERSION" = $BaselineVersion
    "APM_BASELINE_SHA256" = $baselineSha
}
$longResult = Invoke-InstallerPytest -Name "long-root" -OutDir $OutputDir -BaseTemp $longBaseTemp `
    -InstallerEnvironment $installerEnvironment
$shortResult = Invoke-InstallerPytest -Name "short-root" -OutDir $OutputDir -BaseTemp $shortBaseTemp `
    -InstallerEnvironment $installerEnvironment

$summary = [ordered]@{
    candidate = [ordered]@{
        version = $candidateVersion
        archive_sha256 = $candidateSha
        executable_sha256 = $candidateExeSha
        metadata_sha = [string]$metadata.sha
    }
    baseline = [ordered]@{
        version = $BaselineVersion
        archive_sha256 = $baselineSha
    }
    roots = [ordered]@{
        long_base_temp = $longBaseTemp
        short_base_temp = $shortBaseTemp
    }
    long_root_exit_code = $longResult.exit_code
    long_root_timed_out = $longResult.timed_out
    short_root_exit_code = $shortResult.exit_code
    short_root_timed_out = $shortResult.timed_out
    long_root_junit = $longResult.junit
    short_root_junit = $shortResult.junit
    conclusion = if ($shortResult.exit_code -eq 0 -and $longResult.exit_code -ne 0) {
        "short-root-passed-long-root-failed"
    } elseif ($shortResult.exit_code -eq 0 -and $longResult.exit_code -eq 0) {
        "both-roots-passed"
    } else {
        "short-root-failed"
    }
}
Write-JsonFile -Value $summary -Path (Join-Path $OutputDir "summary.json")

if ($shortResult.timed_out -or $shortResult.exit_code -ne 0) {
    Write-Host "Short-root installer proof failed; see $OutputDir" -ForegroundColor Red
    if ($shortResult.timed_out) { exit 124 }
    exit $shortResult.exit_code
}
Write-Host "Windows installer proof completed: $($summary.conclusion)"
