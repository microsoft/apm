param(
    [string]$ExpectedHead,
    [switch]$CleanupOnly
)

$ErrorActionPreference = 'Stop'
$RegistryPath = 'SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock'
$RegistryValue = 'AllowDevelopmentWithoutDevLicense'

if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'This gate may only mutate its disposable GitHub-hosted Windows runner.'
}
$RunnerTemp = (Resolve-Path -LiteralPath $env:RUNNER_TEMP).Path
$Control = Join-Path $RunnerTemp 'apm-native-symlink-evidence'
$StateFile = Join-Path $Control 'state.json'
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$RunnerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User

function Write-State($State) {
    $temporary = Join-Path $Control 'state.pending.json'
    [System.IO.File]::WriteAllText($temporary, ($State | ConvertTo-Json -Depth 8))
    Move-Item -LiteralPath $temporary -Destination $StateFile -Force
}

function Get-DeveloperModeState {
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($RegistryPath)
    if ($null -eq $key) {
        return [ordered]@{ KeyExists = $false; ValueExists = $false; Kind = $null; Value = $null }
    }
    try {
        $exists = $key.GetValueNames() -contains $RegistryValue
        return [ordered]@{
            KeyExists = $true
            ValueExists = $exists
            Kind = $(if ($exists) { $key.GetValueKind($RegistryValue).ToString() } else { $null })
            Value = $(if ($exists) { $key.GetValue($RegistryValue) } else { $null })
        }
    }
    finally { $key.Close() }
}

function Assert-OwnedState($State) {
    if ($State.RunId -ne $env:GITHUB_RUN_ID -or $State.RunAttempt -ne $env:GITHUB_RUN_ATTEMPT -or $State.RunnerSid -ne $RunnerSid.Value) {
        throw 'Native cleanup state does not belong to this runner invocation.'
    }
    if ($State.UserName -notmatch '^apmns[a-f0-9]{12}$') {
        throw 'Invalid owned account name in cleanup state.'
    }
    if ([System.IO.Path]::GetDirectoryName($State.Scratch) -ne $RunnerTemp -or
        [System.IO.Path]::GetFileName($State.Scratch) -notmatch '^apm-native-[a-f0-9]{32}$') {
        throw 'Refusing cleanup outside the exact owned scratch directory.'
    }
}

function Stop-OwnedProcess($State) {
    if (-not $State.ChildPid) { return }
    $process = Get-Process | Where-Object { $_.Id -eq $State.ChildPid }
    if ($null -eq $process) { return }
    if ($process.StartTime.ToUniversalTime().Ticks.ToString() -ne $State.ChildStartTicks) {
        throw 'The recorded PID was reused; refusing to terminate it.'
    }
    $process.Kill($true)
    if (-not $process.WaitForExit(30000)) { throw 'The owned child process did not terminate.' }
}

function Restore-DeveloperMode($State) {
    $before = $State.RegistryBefore | ConvertTo-Json -Compress
    $current = Get-DeveloperModeState
    if (($current | ConvertTo-Json -Compress) -eq $before) { return }
    if (-not $State.RegistryChanged -or -not $current.ValueExists -or $current.Kind -ne 'DWord' -or $current.Value -ne 0) {
        throw 'Developer Mode changed unexpectedly; refusing to overwrite unrelated state.'
    }
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($RegistryPath, $true)
    try {
        $key.SetValue($RegistryValue, [int]$State.RegistryBefore.Value, [Microsoft.Win32.RegistryValueKind]::DWord)
    }
    finally { $key.Close() }
    if (((Get-DeveloperModeState) | ConvertTo-Json -Compress) -ne $before) {
        throw 'Developer Mode key/value presence, type or value was not restored.'
    }
}

function Remove-OwnedProfile($State) {
    if (-not $State.UserSid) { return }
    $profiles = @(Get-CimInstance -ClassName Win32_UserProfile -Filter "SID='$($State.UserSid)'")
    if ($profiles.Count -gt 1) { throw 'More than one profile matches the newly created SID.' }
    foreach ($profile in $profiles) {
        if ($profile.Loaded) { throw 'The owned user profile is still loaded.' }
        $profile | Remove-CimInstance
    }
    if (@(Get-CimInstance -ClassName Win32_UserProfile -Filter "SID='$($State.UserSid)'").Count) {
        throw 'The owned user profile was not removed.'
    }
}

function Remove-OwnedUser($State) {
    $users = @(Get-LocalUser | Where-Object { $_.Name -eq $State.UserName })
    if (-not $users.Count) { return }
    if ($users.Count -ne 1 -or -not $State.UserSid -or $users[0].SID.Value -ne $State.UserSid) {
        throw 'The account identity differs from the recorded created SID; refusing deletion.'
    }
    Remove-LocalUser -SID $users[0].SID
    if (@(Get-LocalUser | Where-Object { $_.Name -eq $State.UserName }).Count) {
        throw 'The owned local account was not removed.'
    }
}

function Save-ChildEvidence($State) {
    if (-not (Test-Path -LiteralPath $State.Scratch)) { return }
    foreach ($name in @('stdout.log', 'stderr.log', 'native-proof.json', 'baseline.xml', 'mutation.xml', 'restored.xml')) {
        $source = Join-Path $State.Scratch $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $Control $name) -Force
        }
    }
}

function Invoke-OwnedCleanup($State) {
    Assert-OwnedState $State
    $failures = [System.Collections.Generic.List[string]]::new()
    $actions = [ordered]@{
        process = { Stop-OwnedProcess $State }
        evidence = { Save-ChildEvidence $State }
        registry = { Restore-DeveloperMode $State }
        profile = { Remove-OwnedProfile $State }
        account = { Remove-OwnedUser $State }
        scratch = {
            if (Test-Path -LiteralPath $State.Scratch) {
                Remove-Item -LiteralPath $State.Scratch -Recurse -Force
            }
            if (Test-Path -LiteralPath $State.Scratch) { throw 'Owned scratch/ACLs remain.' }
        }
    }
    foreach ($entry in $actions.GetEnumerator()) {
        try { & $entry.Value }
        catch { $failures.Add("$($entry.Key): $($_.Exception.Message)") }
    }
    $State.CleanupComplete = $failures.Count -eq 0
    $State.CleanupFailures = @($failures)
    $State.CleanupHistory = @($State.CleanupHistory) + @([ordered]@{
        At = [DateTime]::UtcNow.ToString('o')
        Complete = $State.CleanupComplete
        Failures = @($failures)
    })
    Write-State $State
    if ($failures.Count) {
        throw "Native cleanup failed: $($failures -join '; ')"
    }
    Write-Host '[+] Native account, profile, registry and subject scratch restored.'
}

if ($CleanupOnly) {
    if (-not (Test-Path -LiteralPath $StateFile)) {
        Write-Host '[i] No persisted native setup state; no subject resources were created.'
        exit 0
    }
    $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json -AsHashtable
    Invoke-OwnedCleanup $state
    exit 0
}

if ($ExpectedHead -notmatch '^[0-9a-f]{40}$') { throw 'An exact PR/source SHA is required.' }
if (Test-Path -LiteralPath $Control) { throw 'Native evidence directory already exists; refusing reuse.' }
$principal = [System.Security.Principal.WindowsPrincipal]::new([System.Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Privileged fixture setup requires the hosted runner administrator, never the subject user.'
}
$registryBefore = Get-DeveloperModeState
if ($registryBefore.ValueExists -and $registryBefore.Kind -ne 'DWord') {
    throw 'Unexpected Developer Mode registry type; no machine changes were made.'
}
$nonce = [Guid]::NewGuid().ToString('N')
$userName = 'apmns' + $nonce.Substring(0, 12)
if (@(Get-LocalUser | Where-Object { $_.Name -eq $userName }).Count) { throw 'Account collision.' }
$scratch = Join-Path $RunnerTemp ('apm-native-' + $nonce)
if (Test-Path -LiteralPath $scratch) { throw 'Scratch collision.' }

New-Item -ItemType Directory -Path $Control | Out-Null
$controlAcl = [System.Security.AccessControl.DirectorySecurity]::new()
$controlAcl.SetAccessRuleProtection($true, $false)
$controlAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
    $RunnerSid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'
))
Set-Acl -LiteralPath $Control -AclObject $controlAcl
$state = [ordered]@{
    RunId = $env:GITHUB_RUN_ID
    RunAttempt = $env:GITHUB_RUN_ATTEMPT
    RunnerSid = $RunnerSid.Value
    Head = $ExpectedHead
    UserName = $userName
    UserSid = $null
    Scratch = $scratch
    RegistryBefore = $registryBefore
    RegistryChanged = $false
    ChildPid = $null
    ChildStartTicks = $null
    ChildExit = $null
    Failure = $null
    CleanupComplete = $false
    CleanupFailures = @()
    CleanupHistory = @()
}
Write-State $state
$failure = $null
$cleanupFailure = $null
try {
    $bytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes(24)
    $password = ConvertTo-SecureString ([Convert]::ToBase64String($bytes) + 'aA1!') -AsPlainText -Force
    $user = New-LocalUser -Name $userName -Password $password -AccountNeverExpires -PasswordNeverExpires
    $state.UserSid = $user.SID.Value
    Write-State $state
    $usersGroup = Get-LocalGroup -SID 'S-1-5-32-545'
    if (-not @(Get-LocalGroupMember -Group $usersGroup | Where-Object { $_.SID -eq $user.SID }).Count) {
        Add-LocalGroupMember -Group $usersGroup -Member $user
    }
    if (@(Get-LocalGroupMember -SID 'S-1-5-32-544' | Where-Object { $_.SID -eq $user.SID }).Count) {
        throw 'Created subject unexpectedly belongs to Administrators.'
    }
    if (@(Get-CimInstance -ClassName Win32_UserProfile -Filter "SID='$($user.SID.Value)'").Count) {
        throw 'A profile existed before the created subject was started.'
    }
    if ($registryBefore.ValueExists -and $registryBefore.Value -eq 1) {
        $state.RegistryChanged = $true
        Write-State $state
        $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($RegistryPath, $true)
        try { $key.SetValue($RegistryValue, 0, [Microsoft.Win32.RegistryValueKind]::DWord) }
        finally { $key.Close() }
    }
    New-Item -ItemType Directory -Path $scratch | Out-Null
    $scratchAcl = Get-Acl -LiteralPath $scratch
    $scratchAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        $user.SID, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'
    ))
    Set-Acl -LiteralPath $scratch -AclObject $scratchAcl
    foreach ($name in @('home', 'temp', 'cache', 'appdata', 'localappdata')) {
        New-Item -ItemType Directory -Path (Join-Path $scratch $name) | Out-Null
    }
    $env:HOME = Join-Path $scratch 'home'
    $env:USERPROFILE = $env:HOME
    $env:HOMEDRIVE = [System.IO.Path]::GetPathRoot($env:HOME).TrimEnd('\')
    $env:HOMEPATH = $env:HOME.Substring($env:HOMEDRIVE.Length)
    $env:TEMP = Join-Path $scratch 'temp'
    $env:TMP = $env:TEMP
    $env:UV_CACHE_DIR = Join-Path $scratch 'cache'
    $env:APPDATA = Join-Path $scratch 'appdata'
    $env:LOCALAPPDATA = Join-Path $scratch 'localappdata'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTEST_ADDOPTS = ''
    $env:APM_WINDOWS_NATIVE_STANDARD_USER = $null
    $env:APM_WINDOWS_NATIVE_MUTATION = $null
    $python = Join-Path $Root '.venv\Scripts\python.exe'
    $entry = Join-Path $Root 'scripts\windows_native_symlink_probe_entry.py'
    $credential = [System.Management.Automation.PSCredential]::new("$env:COMPUTERNAME\$userName", $password)
    $arguments = @(
        '-B', "`"$entry`"", '--root', "`"$Root`"", '--scratch', "`"$scratch`"",
        '--expected-sid', $user.SID.Value, '--expected-head', $ExpectedHead
    )
    $process = Start-Process -FilePath $python -ArgumentList $arguments -Credential $credential `
        -WorkingDirectory $scratch -PassThru `
        -RedirectStandardOutput (Join-Path $scratch 'stdout.log') `
        -RedirectStandardError (Join-Path $scratch 'stderr.log')
    $process.Handle | Out-Null
    $state.ChildPid = $process.Id
    $state.ChildStartTicks = $process.StartTime.ToUniversalTime().Ticks.ToString()
    Write-State $state
    if (-not $process.WaitForExit(480000)) { throw 'Native subject exceeded its eight-minute timeout.' }
    $process.Refresh()
    $state.ChildExit = $process.ExitCode
    Write-State $state
    if ($process.ExitCode -ne 0) { throw "Native subject failed with exit $($process.ExitCode)." }
    if (-not (Test-Path -LiteralPath (Join-Path $scratch 'native-proof.json') -PathType Leaf)) {
        throw 'The native subject produced no positive proof.'
    }
}
catch {
    $failure = $_.Exception.Message
    $state.Failure = $failure
}
finally {
    try { Invoke-OwnedCleanup $state }
    catch { $cleanupFailure = $_.Exception.Message }
    foreach ($name in @('stdout.log', 'stderr.log')) {
        $log = Join-Path $Control $name
        if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
    }
}
if ($failure) { Write-Host "[x] $failure" }
if ($cleanupFailure) { Write-Host "[x] $cleanupFailure" }
if ($failure -or $cleanupFailure) { exit 1 }
Write-Host '[+] Native symlink acceptance and independent restoration completed.'
