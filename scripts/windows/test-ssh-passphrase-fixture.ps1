# Hermetic Windows reproduction contract for encrypted SSH private keys.
#
# This script exercises a packaged APM binary through real Git and Windows
# OpenSSH processes. It creates only ephemeral keys and loopback services.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ApmBinary,

    [string]$EvidenceDirectory = (Join-Path $PWD "ssh-passphrase-evidence")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$EvidenceDirectory = [System.IO.Path]::GetFullPath($EvidenceDirectory, $PWD.Path)

function Write-Info {
    param([string]$Message)
    Write-Host "[i] $Message"
}

function Write-Success {
    param([string]$Message)
    Write-Host "[+] $Message"
}

function Assert-Condition {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
    Write-Success $Message
}

function ConvertTo-ForwardSlashPath {
    param([string]$Path)
    return $Path.Replace("\", "/")
}

function Invoke-CapturedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @(),

        [string]$WorkingDirectory = $PWD.Path,

        [System.Collections.IDictionary]$Environment,

        [int]$TimeoutSeconds = 60
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in $ArgumentList) {
        $startInfo.ArgumentList.Add($argument)
    }

    if ($null -ne $Environment) {
        $startInfo.Environment.Clear()
        foreach ($entry in $Environment.GetEnumerator()) {
            if ($null -ne $entry.Value) {
                $startInfo.Environment[[string]$entry.Key] = [string]$entry.Value
            }
        }
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $startedAt = [DateTimeOffset]::UtcNow
    if (-not $process.Start()) {
        throw "Failed to start process: $FilePath"
    }

    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
    if ($timedOut) {
        try {
            $process.Kill($true)
        } catch {
            if (-not $process.HasExited) {
                throw
            }
        }
        $process.WaitForExit()
    }
    $process.WaitForExit()
    $finishedAt = [DateTimeOffset]::UtcNow

    return [pscustomobject]@{
        file_path = $FilePath
        arguments = @($ArgumentList)
        working_directory = $WorkingDirectory
        exit_code = $process.ExitCode
        stdout = $stdoutTask.GetAwaiter().GetResult()
        stderr = $stderrTask.GetAwaiter().GetResult()
        started_at = $startedAt.ToString("o")
        finished_at = $finishedAt.ToString("o")
        duration_ms = [math]::Round(($finishedAt - $startedAt).TotalMilliseconds)
        timed_out = $timedOut
        timeout_seconds = $TimeoutSeconds
    }
}

function Invoke-CheckedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @(),

        [string]$WorkingDirectory = $PWD.Path,

        [System.Collections.IDictionary]$Environment,

        [int]$TimeoutSeconds = 60
    )

    $result = Invoke-CapturedProcess `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -Environment $Environment `
        -TimeoutSeconds $TimeoutSeconds
    if ($result.timed_out -or $result.exit_code -ne 0) {
        throw (
            "Process failed (exit={0}, timed_out={1}): {2}`nstdout:`n{3}`nstderr:`n{4}" -f
            $result.exit_code,
            $result.timed_out,
            $FilePath,
            $result.stdout,
            $result.stderr
        )
    }
    return $result
}

function Get-CleanEnvironment {
    $environment = [ordered]@{}
    foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $environment[[string]$entry.Key] = [string]$entry.Value
    }

    $removeNames = @(
        "ADO_APM_PAT",
        "COPILOT_GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GH_TOKEN",
        "GITHUB_APM_PAT",
        "GITHUB_COPILOT_PAT",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GITHUB_TOKEN",
        "GIT_ASKPASS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_HTTP_EXTRAHEADER",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "SSH_AGENT_PID",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
        "SSH_AUTH_SOCK"
    )
    foreach ($name in $removeNames) {
        $environment.Remove($name)
    }
    foreach ($name in @($environment.Keys)) {
        if (
            $name -like "GITHUB_APM_PAT_*" -or
            $name -like "GIT_CONFIG_KEY_*" -or
            $name -like "GIT_CONFIG_VALUE_*"
        ) {
            $environment.Remove($name)
        }
    }
    return $environment
}

function Write-Utf8Text {
    param(
        [string]$Path,
        [string]$Content
    )
    [System.IO.File]::WriteAllText(
        $Path,
        $Content,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Wait-ForLoopbackPort {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 20
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $connectTask = $client.ConnectAsync("127.0.0.1", $Port)
            if ($connectTask.Wait(500) -and $client.Connected) {
                return
            }
        } catch {
            Start-Sleep -Milliseconds 250
        } finally {
            $client.Dispose()
        }
    }
    throw "Windows OpenSSH did not listen on 127.0.0.1:$Port"
}

function Get-TraceReceipt {
    param([string]$TracePath)

    $events = @()
    if (Test-Path $TracePath) {
        foreach ($line in Get-Content -Path $TracePath) {
            if (-not [string]::IsNullOrWhiteSpace($line)) {
                $events += $line | ConvertFrom-Json
            }
        }
    }

    $transportStarts = @(
        $events | Where-Object {
            $_.event -eq "child_start" -and
            [string]$_.child_class -like "transport/*"
        }
    )
    $transportExits = @(
        $events | Where-Object {
            $_.event -eq "child_exit"
        } | ForEach-Object {
            [pscustomobject]@{
                sid = [string]$_.sid
                child_id = [int]$_.child_id
                pid = [int]$_.pid
                exit_code = [int]$_.code
            }
        }
    )
    $safeEnvironmentNames = @(
        "GIT_TERMINAL_PROMPT",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
        "DISPLAY",
        "SSH_AUTH_SOCK",
        "GIT_SSH_COMMAND"
    )
    $environmentEvents = @(
        $events | Where-Object {
            $_.event -eq "def_param" -and
            $safeEnvironmentNames -contains [string]$_.param
        } | ForEach-Object {
            [pscustomobject]@{
                parameter = [string]$_.param
                value = [string]$_.value
            }
        }
    )

    return [pscustomobject]@{
        event_count = $events.Count
        git_start_commands = @(
            $events | Where-Object { $_.event -eq "start" } | ForEach-Object {
                @($_.argv)
            }
        )
        transport_children = @(
            $transportStarts | ForEach-Object {
                $start = $_
                $matchingExit = $transportExits | Where-Object {
                    $_.sid -eq [string]$start.sid -and
                    $_.child_id -eq [int]$start.child_id
                } | Select-Object -First 1
                [pscustomobject]@{
                    sid = [string]$start.sid
                    child_id = [int]$start.child_id
                    child_class = [string]$start.child_class
                    use_shell = [bool]$start.use_shell
                    argv = @($start.argv)
                    pid = if ($null -ne $matchingExit) { $matchingExit.pid } else { $null }
                    exit_code = if ($null -ne $matchingExit) { $matchingExit.exit_code } else { $null }
                }
            }
        )
        child_exits = $transportExits
        environment = $environmentEvents
    }
}

function Get-AskpassReceipt {
    param([string]$AskpassLog)

    if (-not (Test-Path $AskpassLog)) {
        return @()
    }
    return @(
        Get-Content -Path $AskpassLog | Where-Object {
            -not [string]::IsNullOrWhiteSpace($_)
        } | ForEach-Object {
            $_ | ConvertFrom-Json
        }
    )
}

function New-ScenarioEnvironment {
    param(
        [System.Collections.IDictionary]$BaseEnvironment,
        [string]$ScenarioRoot,
        [string]$IdentityPath,
        [string]$KnownHostsPath,
        [string]$SshPath,
        [string]$SshLogPath,
        [string]$TracePath,
        [string]$AskpassPath,
        [string]$AskpassLogPath,
        [string]$AskpassResponse
    )

    $environment = [ordered]@{}
    foreach ($entry in $BaseEnvironment.GetEnumerator()) {
        $environment[[string]$entry.Key] = [string]$entry.Value
    }

    $home = Join-Path $ScenarioRoot "home"
    $config = Join-Path $ScenarioRoot "config"
    $cache = Join-Path $ScenarioRoot "cache"
    $temp = Join-Path $ScenarioRoot "temp"
    foreach ($path in @($home, $config, $cache, $temp)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
    $updateCache = Join-Path $home "AppData\Local\apm\cache"
    New-Item -ItemType Directory -Path $updateCache -Force | Out-Null
    New-Item -ItemType File -Path (Join-Path $updateCache "last_version_check") -Force | Out-Null

    $sshCommand = @(
        (ConvertTo-ForwardSlashPath $SshPath),
        "-vvv",
        "-E",
        (ConvertTo-ForwardSlashPath $SshLogPath),
        "-i",
        (ConvertTo-ForwardSlashPath $IdentityPath),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=$(ConvertTo-ForwardSlashPath $KnownHostsPath)",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "NumberOfPasswordPrompts=1"
    ) -join " "

    $environment["HOME"] = $home
    $environment["USERPROFILE"] = $home
    $environment["LOCALAPPDATA"] = Join-Path $home "AppData\Local"
    $environment["XDG_CONFIG_HOME"] = $config
    $environment["XDG_CACHE_HOME"] = $cache
    $environment["APM_HOME"] = $config
    $environment["APM_CACHE_DIR"] = $cache
    $environment["APM_TEMP_DIR"] = $temp
    $environment["TEMP"] = $temp
    $environment["TMP"] = $temp
    $environment["APM_NO_CACHE"] = "1"
    $environment["APM_TIERED_RESOLVER"] = "0"
    $environment["APM_PROGRESS"] = "never"
    $environment["APM_GIT_PROTOCOL"] = "ssh"
    $environment["APM_ALLOW_PROTOCOL_FALLBACK"] = "0"
    $environment["CI"] = "1"
    $environment["GIT_CONFIG_NOSYSTEM"] = "1"
    $environment["GIT_CONFIG_SYSTEM"] = "NUL"
    $environment["GIT_CONFIG_GLOBAL"] = (ConvertTo-ForwardSlashPath (Join-Path $ScenarioRoot "empty.gitconfig"))
    $environment["GIT_SSH_COMMAND"] = $sshCommand
    $environment["GIT_TRACE2_EVENT"] = (ConvertTo-ForwardSlashPath $TracePath)
    $environment["GIT_TRACE2_ENV_VARS"] = (
        "GIT_TERMINAL_PROMPT,GIT_ASKPASS,SSH_ASKPASS," +
        "SSH_ASKPASS_REQUIRE,DISPLAY,SSH_AUTH_SOCK,GIT_SSH_COMMAND"
    )
    $environment["HTTP_PROXY"] = "http://127.0.0.1:9"
    $environment["HTTPS_PROXY"] = "http://127.0.0.1:9"
    $environment["ALL_PROXY"] = "http://127.0.0.1:9"
    $environment["NO_PROXY"] = "127.0.0.1,localhost"
    $environment["SSH_ASKPASS_REQUIRE"] = "force"
    $environment["DISPLAY"] = "apm-fixture:0"
    Write-Utf8Text -Path $environment["GIT_CONFIG_GLOBAL"] -Content ""

    if (-not [string]::IsNullOrWhiteSpace($AskpassPath)) {
        $environment["SSH_ASKPASS"] = (ConvertTo-ForwardSlashPath $AskpassPath)
        $environment["APM_SSH_FIXTURE_ASKPASS_LOG"] = $AskpassLogPath
    } else {
        $environment.Remove("SSH_ASKPASS")
        $environment.Remove("APM_SSH_FIXTURE_ASKPASS_LOG")
    }
    if (-not [string]::IsNullOrEmpty($AskpassResponse)) {
        $environment["APM_SSH_FIXTURE_RESPONSE"] = $AskpassResponse
    } else {
        $environment.Remove("APM_SSH_FIXTURE_RESPONSE")
    }
    return $environment
}

function Invoke-Scenario {
    param(
        [string]$Id,
        [bool]$ExpectedSuccess,
        [string]$IdentityPath,
        [string]$RemoteUrl,
        [string]$ExpectedSkillMarker,
        [string]$ApmPath,
        [string]$Root,
        [string]$EvidenceRoot,
        [System.Collections.IDictionary]$BaseEnvironment,
        [string]$KnownHostsPath,
        [string]$SshPath,
        [string]$AskpassPath,
        [string]$AskpassResponse
    )

    $scenarioRoot = Join-Path $Root $Id
    $projectRoot = Join-Path $scenarioRoot "project"
    $scenarioEvidence = Join-Path $EvidenceRoot $Id
    New-Item -ItemType Directory -Path $projectRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $scenarioEvidence -Force | Out-Null

    $tracePath = Join-Path $scenarioEvidence "git-trace.json"
    $sshLogPath = Join-Path $scenarioEvidence "ssh-client.log"
    $askpassLogPath = Join-Path $scenarioEvidence "askpass.jsonl"
    $stdoutPath = Join-Path $scenarioEvidence "apm.stdout.txt"
    $stderrPath = Join-Path $scenarioEvidence "apm.stderr.txt"
    $manifestPath = Join-Path $projectRoot "apm.yml"
    $manifest = @"
name: consumer-$Id
version: 1.0.0
description: Windows encrypted SSH fixture consumer
dependencies:
  apm:
    - git: "$RemoteUrl"
      ref: main
targets:
  - copilot
"@
    Write-Utf8Text -Path $manifestPath -Content $manifest

    $environment = New-ScenarioEnvironment `
        -BaseEnvironment $BaseEnvironment `
        -ScenarioRoot $scenarioRoot `
        -IdentityPath $IdentityPath `
        -KnownHostsPath $KnownHostsPath `
        -SshPath $SshPath `
        -SshLogPath $sshLogPath `
        -TracePath $tracePath `
        -AskpassPath $AskpassPath `
        -AskpassLogPath $askpassLogPath `
        -AskpassResponse $AskpassResponse

    Write-Info "Running scenario $Id through packaged APM"
    $process = Invoke-CapturedProcess `
        -FilePath $ApmPath `
        -ArgumentList @(
            "install",
            "--target",
            "copilot",
            "--no-policy",
            "--parallel-downloads",
            "0"
        ) `
        -WorkingDirectory $projectRoot `
        -Environment $environment `
        -TimeoutSeconds 120
    Write-Utf8Text -Path $stdoutPath -Content $process.stdout
    Write-Utf8Text -Path $stderrPath -Content $process.stderr

    $skillPath = Join-Path $projectRoot ".github\skills\ssh-passphrase-fixture\SKILL.md"
    $lockPath = Join-Path $projectRoot "apm.lock.yaml"
    $actualSuccess = -not $process.timed_out -and $process.exit_code -eq 0
    $skillInstalled = Test-Path $skillPath
    $lockWritten = Test-Path $lockPath
    $askpassReceipt = Get-AskpassReceipt -AskpassLog $askpassLogPath
    $traceReceipt = Get-TraceReceipt -TracePath $tracePath
    $skillMatches = $false
    if ($skillInstalled) {
        $skillMatches = (Get-Content -Path $skillPath -Raw).Contains($ExpectedSkillMarker)
    }

    $receipt = [pscustomobject]@{
        id = $Id
        expected_success = $ExpectedSuccess
        actual_success = $actualSuccess
        expectation_met = $actualSuccess -eq $ExpectedSuccess
        apm_process = $process
        environment_contract = [pscustomobject]@{
            git_ssh_command = [string]$environment["GIT_SSH_COMMAND"]
            ssh_askpass_present = $environment.Contains("SSH_ASKPASS")
            ssh_askpass_require = [string]$environment["SSH_ASKPASS_REQUIRE"]
            display = [string]$environment["DISPLAY"]
            ssh_auth_sock_present = $environment.Contains("SSH_AUTH_SOCK")
            git_terminal_prompt_input = $environment.Contains("GIT_TERMINAL_PROMPT")
            git_askpass_input = $environment.Contains("GIT_ASKPASS")
            all_proxy = [string]$environment["ALL_PROXY"]
            no_proxy = [string]$environment["NO_PROXY"]
        }
        askpass_invocation_count = $askpassReceipt.Count
        askpass_invocations = $askpassReceipt
        git_trace = $traceReceipt
        skill_installed = $skillInstalled
        skill_marker_matches = $skillMatches
        lock_written = $lockWritten
    }
    Write-Utf8Text `
        -Path (Join-Path $scenarioEvidence "receipt.json") `
        -Content ($receipt | ConvertTo-Json -Depth 20)
    return $receipt
}

$root = Join-Path $env:RUNNER_TEMP ("apm-ssh-passphrase-" + [guid]::NewGuid().ToString("N"))
$keysRoot = Join-Path $root "keys"
$sourceRoot = Join-Path $root "source"
$bareRoot = Join-Path $root "ssh-passphrase-fixture.git"
$serverCommandLog = Join-Path $EvidenceDirectory "server-commands.log"
$askpassSource = Join-Path $root "askpass.go"
$askpassExe = Join-Path $root "askpass.exe"
$knownHosts = Join-Path $root "known_hosts"
$encryptedKey = Join-Path $keysRoot "id_ed25519_encrypted"
$plainKey = Join-Path $keysRoot "id_ed25519_plain"
$passphrase = "apm-" + [guid]::NewGuid().ToString("N")
$wrongPassphrase = "wrong-" + [guid]::NewGuid().ToString("N")
$port = 22222
$expectedSkillMarker = "windows-ssh-passphrase-fixture"
$programDataSsh = Join-Path $env:ProgramData "ssh"
$sshdConfig = Join-Path $programDataSsh "sshd_config"
$authorizedKeys = Join-Path $programDataSsh "administrators_authorized_keys"
$sshdLogRoot = Join-Path $programDataSsh "logs"
$sshdLogDestination = Join-Path $EvidenceDirectory "sshd"
$sshdService = $null
$initialServiceStatus = $null
$initialServiceStartType = $null
$originalSshdConfig = $null
$originalAuthorizedKeys = $null
$sshdConfigExisted = Test-Path $sshdConfig
$authorizedKeysExisted = Test-Path $authorizedKeys
$assertionFailures = [System.Collections.Generic.List[string]]::new()

try {
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    New-Item -ItemType Directory -Path $keysRoot -Force | Out-Null
    if (Test-Path $EvidenceDirectory) {
        Remove-Item -Path $EvidenceDirectory -Recurse -Force
    }
    New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null

    $apmPath = (Resolve-Path $ApmBinary).Path
    $gitPath = (Get-Command git.exe -ErrorAction Stop).Source
    $goPath = (Get-Command go.exe -ErrorAction Stop).Source
    $sshPath = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
    $sshKeygenPath = Join-Path $env:WINDIR "System32\OpenSSH\ssh-keygen.exe"
    $sshKeyscanPath = Join-Path $env:WINDIR "System32\OpenSSH\ssh-keyscan.exe"
    $sshdPath = Join-Path $env:WINDIR "System32\OpenSSH\sshd.exe"
    foreach ($requiredPath in @(
        $apmPath,
        $gitPath,
        $goPath,
        $sshPath,
        $sshKeygenPath,
        $sshKeyscanPath,
        $sshdPath
    )) {
        Assert-Condition (Test-Path $requiredPath -PathType Leaf) "Required executable exists: $requiredPath"
    }
    Assert-Condition ($apmPath.EndsWith("apm.exe")) "Fixture uses a packaged apm.exe"
    Assert-Condition (-not ($root -match "\s")) "Ephemeral fixture path contains no whitespace"

    $principal = [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    Assert-Condition (
        $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    ) "Hosted runner account is an administrator"

    $baseEnvironment = Get-CleanEnvironment
    $gitVersion = Invoke-CheckedProcess -FilePath $gitPath -ArgumentList @("--version")
    $sshVersion = Invoke-CheckedProcess -FilePath $sshPath -ArgumentList @("-V")
    $sshdVersion = Invoke-CheckedProcess -FilePath $sshdPath -ArgumentList @("-V")
    $apmVersion = Invoke-CheckedProcess -FilePath $apmPath -ArgumentList @("--version")

    Write-Info "Creating ephemeral Git package and bare origin"
    New-Item -ItemType Directory -Path (Join-Path $sourceRoot "skills\ssh-passphrase-fixture") -Force |
        Out-Null
    Write-Utf8Text -Path (Join-Path $sourceRoot "apm.yml") -Content @"
name: ssh-passphrase-fixture
version: 1.0.0
description: Hermetic Windows SSH passphrase fixture
targets:
  - copilot
"@
    Write-Utf8Text `
        -Path (Join-Path $sourceRoot "skills\ssh-passphrase-fixture\SKILL.md") `
        -Content @"
---
name: ssh-passphrase-fixture
description: Hermetic Windows SSH passphrase fixture
---
# $expectedSkillMarker
"@
    Invoke-CheckedProcess -FilePath $gitPath -ArgumentList @("init", "-b", "main") -WorkingDirectory $sourceRoot |
        Out-Null
    Invoke-CheckedProcess -FilePath $gitPath -ArgumentList @("config", "user.name", "APM Fixture") -WorkingDirectory $sourceRoot |
        Out-Null
    Invoke-CheckedProcess -FilePath $gitPath -ArgumentList @("config", "user.email", "fixture@invalid.example") -WorkingDirectory $sourceRoot |
        Out-Null
    Invoke-CheckedProcess -FilePath $gitPath -ArgumentList @("add", ".") -WorkingDirectory $sourceRoot |
        Out-Null
    Invoke-CheckedProcess -FilePath $gitPath -ArgumentList @("commit", "-m", "seed fixture") -WorkingDirectory $sourceRoot |
        Out-Null
    Invoke-CheckedProcess -FilePath $gitPath -ArgumentList @("clone", "--bare", $sourceRoot, $bareRoot) |
        Out-Null
    $fixtureCommit = (
        Invoke-CheckedProcess -FilePath $gitPath -ArgumentList @("-C", $sourceRoot, "rev-parse", "HEAD")
    ).stdout.Trim()

    Write-Info "Generating one encrypted and one unencrypted private key"
    Invoke-CheckedProcess `
        -FilePath $sshKeygenPath `
        -ArgumentList @("-q", "-t", "ed25519", "-N", $passphrase, "-f", $encryptedKey) |
        Out-Null
    Invoke-CheckedProcess `
        -FilePath $sshKeygenPath `
        -ArgumentList @("-q", "-t", "ed25519", "-N", "", "-f", $plainKey) |
        Out-Null
    $encryptedFingerprint = (
        Invoke-CheckedProcess -FilePath $sshKeygenPath -ArgumentList @("-lf", "$encryptedKey.pub")
    ).stdout.Trim()
    $plainFingerprint = (
        Invoke-CheckedProcess -FilePath $sshKeygenPath -ArgumentList @("-lf", "$plainKey.pub")
    ).stdout.Trim()

    $askpassProgram = @'
package main

import (
    "encoding/json"
    "fmt"
    "os"
)

type invocation struct {
    Arguments          []string `json:"arguments"`
    GitTerminalPrompt  string   `json:"git_terminal_prompt"`
    GitAskpass         string   `json:"git_askpass"`
    SshAskpass         string   `json:"ssh_askpass"`
    SshAskpassRequire  string   `json:"ssh_askpass_require"`
    Display            string   `json:"display"`
    SshAuthSockPresent bool     `json:"ssh_auth_sock_present"`
    ResponsePresent    bool     `json:"response_present"`
}

func main() {
    response, responsePresent := os.LookupEnv("APM_SSH_FIXTURE_RESPONSE")
    record := invocation{
        Arguments:          os.Args[1:],
        GitTerminalPrompt:  os.Getenv("GIT_TERMINAL_PROMPT"),
        GitAskpass:         os.Getenv("GIT_ASKPASS"),
        SshAskpass:         os.Getenv("SSH_ASKPASS"),
        SshAskpassRequire:  os.Getenv("SSH_ASKPASS_REQUIRE"),
        Display:            os.Getenv("DISPLAY"),
        SshAuthSockPresent: os.Getenv("SSH_AUTH_SOCK") != "",
        ResponsePresent:    responsePresent,
    }
    logPath := os.Getenv("APM_SSH_FIXTURE_ASKPASS_LOG")
    if logPath != "" {
        logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
        if err != nil {
            os.Exit(3)
        }
        encoder := json.NewEncoder(logFile)
        if err := encoder.Encode(record); err != nil {
            logFile.Close()
            os.Exit(4)
        }
        if err := logFile.Close(); err != nil {
            os.Exit(5)
        }
    }
    if !responsePresent {
        os.Exit(2)
    }
    fmt.Fprintln(os.Stdout, response)
}
'@
    Write-Utf8Text -Path $askpassSource -Content $askpassProgram
    Invoke-CheckedProcess `
        -FilePath $goPath `
        -ArgumentList @("build", "-trimpath", "-ldflags=-s -w", "-o", $askpassExe, $askpassSource) `
        -WorkingDirectory $root `
        -Environment $baseEnvironment `
        -TimeoutSeconds 120 |
        Out-Null

    Write-Info "Configuring Windows OpenSSH on loopback only"
    $sshdService = Get-Service -Name sshd -ErrorAction Stop
    $initialServiceStatus = $sshdService.Status
    $initialServiceStartType = $sshdService.StartType
    if ($sshdService.Status -eq "Running") {
        Stop-Service -Name sshd -Force
    }
    Set-Service -Name sshd -StartupType Manual
    New-Item -ItemType Directory -Path $programDataSsh -Force | Out-Null
    if ($sshdConfigExisted) {
        $originalSshdConfig = [System.IO.File]::ReadAllBytes($sshdConfig)
    }
    if ($authorizedKeysExisted) {
        $originalAuthorizedKeys = [System.IO.File]::ReadAllBytes($authorizedKeys)
    }

    Invoke-CheckedProcess -FilePath $sshKeygenPath -ArgumentList @("-A") | Out-Null
    $serveCommand = Join-Path $root "serve-git.cmd"
    $gitForward = ConvertTo-ForwardSlashPath $gitPath
    $bareForward = ConvertTo-ForwardSlashPath $bareRoot
    $serverLogForward = ConvertTo-ForwardSlashPath $serverCommandLog
    Write-Utf8Text -Path $serveCommand -Content @"
@echo off
echo %SSH_ORIGINAL_COMMAND%>>"$serverLogForward"
"$gitForward" upload-pack "$bareForward"
"@
    $serveCommandForward = ConvertTo-ForwardSlashPath $serveCommand
    $keyOptions = (
        'command="' + $serveCommandForward +
        '",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding '
    )
    $authorizedContent = @(
        $keyOptions + (Get-Content -Path "$encryptedKey.pub" -Raw).Trim(),
        $keyOptions + (Get-Content -Path "$plainKey.pub" -Raw).Trim()
    ) -join "`n"
    Write-Utf8Text -Path $authorizedKeys -Content ($authorizedContent + "`n")
    & icacls.exe $authorizedKeys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to secure administrators_authorized_keys"
    }

    $runnerUser = $env:USERNAME.ToLowerInvariant()
    $sshdConfiguration = @"
Port $port
ListenAddress 127.0.0.1
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
AllowUsers $runnerUser
AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
LogLevel DEBUG3
SyslogFacility LOCAL0
Subsystem sftp sftp-server.exe
Match Group administrators
    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
"@
    Write-Utf8Text -Path $sshdConfig -Content $sshdConfiguration
    Invoke-CheckedProcess -FilePath $sshdPath -ArgumentList @("-t", "-f", $sshdConfig) | Out-Null
    Start-Service -Name sshd
    Wait-ForLoopbackPort -Port $port

    $keyscan = Invoke-CheckedProcess `
        -FilePath $sshKeyscanPath `
        -ArgumentList @("-p", [string]$port, "127.0.0.1")
    Write-Utf8Text -Path $knownHosts -Content $keyscan.stdout
    Assert-Condition (-not [string]::IsNullOrWhiteSpace($keyscan.stdout)) "Loopback SSH host key was captured"

    $remoteUrl = "ssh://${runnerUser}@127.0.0.1:${port}/fixture/ssh-passphrase-fixture.git"
    $scenarioDefinitions = @(
        [pscustomobject]@{
            id = "unencrypted-positive"
            expected_success = $true
            identity = $plainKey
            askpass = ""
            response = ""
        },
        [pscustomobject]@{
            id = "encrypted-correct"
            expected_success = $true
            identity = $encryptedKey
            askpass = $askpassExe
            response = $passphrase
        },
        [pscustomobject]@{
            id = "encrypted-wrong"
            expected_success = $false
            identity = $encryptedKey
            askpass = $askpassExe
            response = $wrongPassphrase
        },
        [pscustomobject]@{
            id = "encrypted-missing"
            expected_success = $false
            identity = $encryptedKey
            askpass = (Join-Path $root "missing-askpass.exe")
            response = ""
        }
    )

    $scenarioReceipts = @()
    foreach ($scenario in $scenarioDefinitions) {
        $receipt = Invoke-Scenario `
            -Id $scenario.id `
            -ExpectedSuccess $scenario.expected_success `
            -IdentityPath $scenario.identity `
            -RemoteUrl $remoteUrl `
            -ExpectedSkillMarker $expectedSkillMarker `
            -ApmPath $apmPath `
            -Root $root `
            -EvidenceRoot $EvidenceDirectory `
            -BaseEnvironment $baseEnvironment `
            -KnownHostsPath $knownHosts `
            -SshPath $sshPath `
            -AskpassPath $scenario.askpass `
            -AskpassResponse $scenario.response
        $scenarioReceipts += $receipt
    }

    foreach ($receipt in $scenarioReceipts) {
        if ($receipt.apm_process.timed_out) {
            $assertionFailures.Add(
                "Scenario $($receipt.id): packaged APM timed out after " +
                "$($receipt.apm_process.timeout_seconds) seconds"
            )
        }
        if (-not $receipt.expectation_met) {
            $assertionFailures.Add(
                "Scenario {0}: expected_success={1}, exit_code={2}" -f
                $receipt.id,
                $receipt.expected_success,
                $receipt.apm_process.exit_code
            )
        }
        if ($receipt.git_trace.transport_children.Count -eq 0) {
            $assertionFailures.Add("Scenario $($receipt.id): no Git SSH transport child recorded")
        } else {
            $expectedSshPath = ConvertTo-ForwardSlashPath $sshPath
            $openSshChildren = @(
                $receipt.git_trace.transport_children | Where-Object {
                    $_.child_class -eq "transport/ssh" -and
                    ((@($_.argv) -join " ") -replace "\\", "/").Contains($expectedSshPath)
                }
            )
            if ($openSshChildren.Count -eq 0) {
                $assertionFailures.Add(
                    "Scenario $($receipt.id): Git did not record the configured Windows OpenSSH child"
                )
            } else {
                $missingChildExits = @(
                    $openSshChildren | Where-Object { $null -eq $_.exit_code }
                )
                if ($missingChildExits.Count -gt 0) {
                    $assertionFailures.Add(
                        "Scenario $($receipt.id): one or more OpenSSH children have no Trace2 exit"
                    )
                }
                $sshExitCodes = @(
                    $openSshChildren | Where-Object { $null -ne $_.exit_code } |
                        ForEach-Object { [int]$_.exit_code }
                )
                if ($receipt.expected_success -and @($sshExitCodes | Where-Object { $_ -ne 0 }).Count -gt 0) {
                    $assertionFailures.Add(
                        "Scenario $($receipt.id): a successful case recorded a failed OpenSSH child"
                    )
                }
                if (
                    -not $receipt.expected_success -and
                    @($sshExitCodes | Where-Object { $_ -ne 0 }).Count -eq 0
                ) {
                    $assertionFailures.Add(
                        "Scenario $($receipt.id): negative case recorded no failed OpenSSH child"
                    )
                }
            }
        }
        $traceEnvironment = @{}
        foreach ($entry in $receipt.git_trace.environment) {
            $traceEnvironment[[string]$entry.parameter] = [string]$entry.value
        }
        foreach ($expectedPair in @{
            "GIT_TERMINAL_PROMPT" = "0"
            "SSH_ASKPASS_REQUIRE" = "force"
            "DISPLAY" = "apm-fixture:0"
        }.GetEnumerator()) {
            if ($traceEnvironment[[string]$expectedPair.Key] -ne [string]$expectedPair.Value) {
                $assertionFailures.Add(
                    "Scenario $($receipt.id): Trace2 did not record " +
                    "$($expectedPair.Key)=$($expectedPair.Value)"
                )
            }
        }
        if (-not $traceEnvironment.ContainsKey("GIT_SSH_COMMAND")) {
            $assertionFailures.Add(
                "Scenario $($receipt.id): Trace2 did not record GIT_SSH_COMMAND"
            )
        }
        if ($traceEnvironment.ContainsKey("GIT_ASKPASS")) {
            $assertionFailures.Add(
                "Scenario $($receipt.id): generic SSH child unexpectedly retained GIT_ASKPASS"
            )
        }
        if ($traceEnvironment.ContainsKey("SSH_AUTH_SOCK")) {
            $assertionFailures.Add(
                "Scenario $($receipt.id): SSH agent state leaked into the Git child"
            )
        }
        $expectsAskpassEnvironment = $receipt.id -ne "unencrypted-positive"
        if ($traceEnvironment.ContainsKey("SSH_ASKPASS") -ne $expectsAskpassEnvironment) {
            $assertionFailures.Add(
                "Scenario $($receipt.id): Trace2 SSH_ASKPASS presence did not match the case"
            )
        }
        if ($receipt.expected_success) {
            if (-not $receipt.skill_installed) {
                $assertionFailures.Add("Scenario $($receipt.id): expected skill was not installed")
            }
            if (-not $receipt.skill_marker_matches) {
                $assertionFailures.Add("Scenario $($receipt.id): installed skill marker did not match")
            }
            if (-not $receipt.lock_written) {
                $assertionFailures.Add("Scenario $($receipt.id): expected lockfile was not written")
            }
        } else {
            if ($receipt.skill_installed) {
                $assertionFailures.Add("Scenario $($receipt.id): failed install materialized a skill")
            }
            if ($receipt.lock_written) {
                $assertionFailures.Add("Scenario $($receipt.id): failed install wrote a lockfile")
            }
        }
    }

    $plainReceipt = $scenarioReceipts | Where-Object { $_.id -eq "unencrypted-positive" }
    $correctReceipt = $scenarioReceipts | Where-Object { $_.id -eq "encrypted-correct" }
    $wrongReceipt = $scenarioReceipts | Where-Object { $_.id -eq "encrypted-wrong" }
    $missingReceipt = $scenarioReceipts | Where-Object { $_.id -eq "encrypted-missing" }
    if ($plainReceipt.askpass_invocation_count -ne 0) {
        $assertionFailures.Add("Unencrypted positive unexpectedly invoked askpass")
    }
    if ($correctReceipt.askpass_invocation_count -lt 1) {
        $assertionFailures.Add("Encrypted correct case did not invoke askpass")
    }
    if ($wrongReceipt.askpass_invocation_count -lt 1) {
        $assertionFailures.Add("Encrypted wrong case did not invoke askpass")
    }
    if ($missingReceipt.askpass_invocation_count -ne 0) {
        $assertionFailures.Add("Encrypted missing case unexpectedly produced an askpass receipt")
    }
    foreach ($receipt in @($correctReceipt, $wrongReceipt)) {
        foreach ($invocation in $receipt.askpass_invocations) {
            if ($invocation.git_terminal_prompt -ne "0") {
                $assertionFailures.Add(
                    "Scenario $($receipt.id): askpass child did not inherit GIT_TERMINAL_PROMPT=0"
                )
            }
            if (-not [string]::IsNullOrEmpty($invocation.git_askpass)) {
                $assertionFailures.Add(
                    "Scenario $($receipt.id): askpass child unexpectedly inherited GIT_ASKPASS"
                )
            }
            if ($invocation.ssh_askpass_require -ne "force") {
                $assertionFailures.Add(
                    "Scenario $($receipt.id): askpass child did not inherit SSH_ASKPASS_REQUIRE=force"
                )
            }
            if ($invocation.display -ne "apm-fixture:0") {
                $assertionFailures.Add(
                    "Scenario $($receipt.id): askpass child did not inherit the fixture DISPLAY"
                )
            }
            if ($invocation.ssh_auth_sock_present) {
                $assertionFailures.Add(
                    "Scenario $($receipt.id): askpass child inherited SSH agent state"
                )
            }
            if (-not $invocation.response_present) {
                $assertionFailures.Add(
                    "Scenario $($receipt.id): askpass child had no ephemeral response"
                )
            }
            if ((@($invocation.arguments) -join " ") -notmatch "(?i)passphrase") {
                $assertionFailures.Add(
                    "Scenario $($receipt.id): askpass invocation was not a key passphrase prompt"
                )
            }
        }
    }

    $summary = [pscustomobject]@{
        issue = 1976
        source_head = (Invoke-CheckedProcess -FilePath $gitPath -ArgumentList @("rev-parse", "HEAD")).stdout.Trim()
        fixture_commit = $fixtureCommit
        platform = [pscustomobject]@{
            os = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
            architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
            powershell = $PSVersionTable.PSVersion.ToString()
        }
        packaged_apm = [pscustomobject]@{
            path = $apmPath
            sha256 = (Get-FileHash -Path $apmPath -Algorithm SHA256).Hash.ToLowerInvariant()
            version_stdout = $apmVersion.stdout.Trim()
            version_stderr = $apmVersion.stderr.Trim()
        }
        tools = [pscustomobject]@{
            git_path = $gitPath
            git_version = $gitVersion.stdout.Trim()
            ssh_path = $sshPath
            ssh_version = ($sshVersion.stderr + $sshVersion.stdout).Trim()
            sshd_path = $sshdPath
            sshd_version = ($sshdVersion.stderr + $sshdVersion.stdout).Trim()
        }
        boundary = [pscustomobject]@{
            address = "127.0.0.1"
            port = $port
            remote_url = $remoteUrl
            encrypted_key_fingerprint = $encryptedFingerprint
            unencrypted_key_fingerprint = $plainFingerprint
            stored_credentials = $false
            external_git_remote = $false
        }
        scenarios = $scenarioReceipts
        assertion_failures = @($assertionFailures)
    }
    $summaryPath = Join-Path $EvidenceDirectory "summary.json"
    Write-Utf8Text -Path $summaryPath -Content ($summary | ConvertTo-Json -Depth 25)

    foreach ($sensitiveValue in @($passphrase, $wrongPassphrase)) {
        foreach ($file in Get-ChildItem -Path $EvidenceDirectory -File -Recurse) {
            if ((Get-Content -Path $file.FullName -Raw).Contains($sensitiveValue)) {
                throw "Ephemeral passphrase leaked into evidence file: $($file.FullName)"
            }
        }
    }

    if ($assertionFailures.Count -gt 0) {
        throw (
            "Windows SSH passphrase contract failed:`n- " +
            ($assertionFailures -join "`n- ")
        )
    }
    Write-Success "Windows SSH passphrase contract passed all four scenarios"
} finally {
    if ($null -ne $sshdService) {
        try {
            Stop-Service -Name sshd -Force -ErrorAction SilentlyContinue
            if ($sshdConfigExisted) {
                [System.IO.File]::WriteAllBytes($sshdConfig, $originalSshdConfig)
            } else {
                Remove-Item -Path $sshdConfig -Force -ErrorAction SilentlyContinue
            }
            if ($authorizedKeysExisted) {
                [System.IO.File]::WriteAllBytes($authorizedKeys, $originalAuthorizedKeys)
            } else {
                Remove-Item -Path $authorizedKeys -Force -ErrorAction SilentlyContinue
            }
            if ($initialServiceStatus -eq "Running") {
                Start-Service -Name sshd
            }
            if ($null -ne $initialServiceStartType) {
                Set-Service -Name sshd -StartupType $initialServiceStartType
            }
        } catch {
            Write-Host "[!] Failed to restore Windows OpenSSH state: $($_.Exception.Message)"
        }
    }
    if (Test-Path $sshdLogRoot) {
        New-Item -ItemType Directory -Path $sshdLogDestination -Force | Out-Null
        Copy-Item -Path (Join-Path $sshdLogRoot "*") -Destination $sshdLogDestination -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $root) {
        Remove-Item -Path $root -Recurse -Force -ErrorAction SilentlyContinue
    }
}
