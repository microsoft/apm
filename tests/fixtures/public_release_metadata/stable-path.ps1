# Execute the production stable-path promotion with filesystem and PATH effects recorded.
$ErrorActionPreference = "Stop"
$installRoot = $env:TEST_INSTALL_ROOT
$binDir = Join-Path $installRoot "bin"
$releaseDir = Join-Path $installRoot "releases/v99.0.0"
$expectedCurrent = Join-Path $installRoot "current"
$expectedExe = Join-Path $expectedCurrent "apm.exe"
$junctions = @{}
$pathEntries = [System.Collections.Generic.List[string]]::new()
$script:checkedExecutable = $false

function New-Item {
    param([string]$ItemType, [string]$Path, [string]$Target)
    if ($ItemType -ne "Junction" -or $Target -ne $releaseDir) { throw "Unexpected junction target" }
    $junctions[$Path] = $Target
}
function Move-Item {
    param([string]$Path, [string]$Destination, [switch]$Force)
    if (-not $junctions.ContainsKey($Path)) { throw "Unexpected promotion source" }
    $junctions[$Destination] = $junctions[$Path]
    $junctions.Remove($Path)
}
function Test-Path {
    param([string]$Path)
    if ($Path -eq $expectedExe) {
        $script:checkedExecutable = $true
        return $junctions.ContainsKey($expectedCurrent) -and $junctions[$expectedCurrent] -eq $releaseDir
    }
    return $junctions.ContainsKey($Path)
}
function Set-Content {
    param([string]$Path, [string]$Value, [string]$Encoding, [switch]$NoNewline)
    if ($Path -ne (Join-Path $binDir "apm.cmd") -or $Encoding -ne "ASCII") {
        throw "Unexpected shim destination"
    }
}
function Add-ToUserPath {
    param([string]$PathEntry)
    $pathEntries.Insert(0, $PathEntry)
}
function Write-ErrorText { param([string]$Message) throw $Message }
function Write-ManualInstallHelp { throw "Unexpected install failure" }

$source = Get-Content -Raw -LiteralPath "$env:TEST_ROOT/install.ps1"
$start = $source.IndexOf('    $currentDir = Join-Path $installRoot')
$endMarker = '    Add-ToUserPath -PathEntry $currentDir'
$end = $source.IndexOf($endMarker, $start) + $endMarker.Length
if ($start -lt 0 -or $end -lt $start) { throw "Stable-path production block not found" }
. ([scriptblock]::Create($source.Substring($start, $end - $start)))
@{
    current_dir = $currentDir
    current_exe = $currentExe
    junction_target = $junctions[$expectedCurrent]
    executable_checked = $script:checkedExecutable
    path_entries = @($pathEntries)
} | ConvertTo-Json -Compress
