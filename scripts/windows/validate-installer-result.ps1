# Require executed installer assertions before qualifying a candidate.
param(
    [Parameter(Mandatory = $true)][string]$ValidateInstallerJUnit,
    [int]$InstallerExitCode = 0
)

$ErrorActionPreference = "Stop"

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

Read-InstallerOutcome -JUnitPath $ValidateInstallerJUnit -ExitCode $InstallerExitCode |
    ConvertTo-Json
