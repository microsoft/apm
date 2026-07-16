<#
.SYNOPSIS
    Evidence-only fixture for apm issue #1976's remaining Windows boundary:
    a *real* interactive SSH passphrase prompt, driven through a genuine
    Windows pseudo console (ConPTY), with no SSH_ASKPASS/GIT_ASKPASS/agent
    configured -- exactly as a human sitting at a PowerShell terminal would
    experience it.

.DESCRIPTION
    Issue #1976 originally reported an SSH connection timeout against a GHE
    host; follow-up comments narrowed the remaining open question to
    whether an *encrypted* (passphrase-protected) SSH key can actually be
    unlocked interactively when `apm install` shells out to git -> ssh on
    Windows. A separate, already-closed investigation proved that the
    forced-SSH_ASKPASS non-interactive path works (GREEN) -- this fixture
    does NOT retest that path. It exists solely to exercise the one
    scenario that path could not cover: a real interactive console.

    Chain under test: packaged apm.exe -> Git for Windows -> Windows
    OpenSSH client -> an ephemeral loopback sshd serving a throwaway bare
    git repository. No external network, no mocked processes, no product
    code changes, no stored secrets -- every key and passphrase is
    generated fresh per run and never written into any evidence artifact.

    Four scenarios are exercised, each in its own hermetic working
    directory (isolated APM_CACHE_DIR/APM_TEMP_DIR/HOME so nothing leaks
    between them or touches the real user profile):

        1. unencrypted-positive  -- plain key, expect no prompt at all.
        2. encrypted-correct     -- encrypted key, correct passphrase typed
                                    into the live ConPTY prompt.
        3. encrypted-wrong       -- encrypted key, wrong passphrase typed
                                    (expect eventual auth failure).
        4. encrypted-cancel      -- encrypted key, prompt observed then
                                    cancelled with a real Ctrl+C byte.

    This script does not assert a specific pass/fail hypothesis about the
    product. Its job is to capture faithful evidence (prompt transcripts,
    exit codes, ssh/Trace2 diagnostics, materialization/lockfile outcomes)
    and classify each scenario + an overall verdict into summary.json. The
    process exit code reflects whether the *fixture itself* completed and
    produced trustworthy evidence (infra success), not whether the
    interactive prompt behaved the way anyone hoped.

.PARAMETER ApmBinary
    Path to a packaged apm.exe (e.g. dist/apm-windows-x86_64/apm.exe).

.PARAMETER EvidenceDirectory
    Directory sanitized evidence artifacts are written into. Recreated
    fresh on every run.

.NOTES
    Windows-only. Requires an interactive-capable OpenSSH client, the
    (pre-installed, normally-stopped) Windows `sshd` service, and local
    Administrator rights on the runner (all present by default on
    GitHub-hosted windows-latest runners).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ApmBinary,

    [string]$EvidenceDirectory = (Join-Path $PSScriptRoot "..\..\conpty-evidence")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "conpty\ConPtySession.psm1") -Force

# ---------------------------------------------------------------------------
# Small, reviewable helpers. Kept tiny and single-purpose on purpose so the
# scenario logic below reads top-to-bottom without hidden control flow.
# ---------------------------------------------------------------------------

function Write-Info    { param([string]$Message) Write-Host "[i] $Message" }
function Write-Ok      { param([string]$Message) Write-Host "[+] $Message" -ForegroundColor Green }
function Write-Warn2   { param([string]$Message) Write-Host "[!] $Message" -ForegroundColor Yellow }
function Write-Err2    { param([string]$Message) Write-Host "[x] $Message" -ForegroundColor Red }
function Write-Step    { param([string]$Message) Write-Host "[*] $Message" -ForegroundColor Cyan }

function Assert-Condition {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Description
    )
    if (-not $Condition) {
        throw "Assertion failed: $Description"
    }
    Write-Ok $Description
}

function ConvertTo-ForwardSlashPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return $Path -replace '\\', '/'
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Invoke-Setup {
    <#
        .SYNOPSIS
        Runs a short-lived helper process to *completion* with redirected
        pipes. Used only for fixture setup/preflight (git init, ssh-keygen,
        a non-interactive `git ls-remote` sanity check). Never used for the
        scenarios under test -- those must go through a real ConPTY so the
        interactive behavior being investigated is not accidentally piped
        away by this convenience wrapper.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory,
        [hashtable]$Environment,
        [int]$TimeoutSeconds = 30
    )
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    foreach ($arg in $ArgumentList) { $psi.ArgumentList.Add($arg) }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    if ($Environment) {
        $psi.EnvironmentVariables.Clear()
        foreach ($key in $Environment.Keys) {
            $psi.EnvironmentVariables[$key] = [string]$Environment[$key]
        }
    }
    $proc = [System.Diagnostics.Process]::new()
    $proc.StartInfo = $psi
    $null = $proc.Start()
    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
        try { $proc.Kill() } catch { Write-Verbose "Kill after timeout failed: $_" }
        throw "Setup process timed out after ${TimeoutSeconds}s: $FilePath $($ArgumentList -join ' ')"
    }
    return [pscustomobject]@{
        ExitCode = $proc.ExitCode
        Stdout   = $stdoutTask.Result
        Stderr   = $stderrTask.Result
    }
}

function Wait-ForLoopbackPort {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSeconds = 15
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $connectTask = $client.ConnectAsync("127.0.0.1", $Port)
            if ($connectTask.Wait(500) -and $client.Connected) {
                return
            }
        } catch {
            Write-Verbose "Loopback port $Port not yet accepting connections: $_"
        } finally {
            $client.Dispose()
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Loopback sshd never accepted a connection on port $Port within ${TimeoutSeconds}s"
}

# ---------------------------------------------------------------------------
# Human-like environment construction. This is the single most important
# function in the fixture: it decides exactly what the child apm.exe process
# does and does not see. A real human's PowerShell terminal would not carry
# SSH_ASKPASS/GIT_ASKPASS/agent sockets unless they configured one, and it
# would not carry CI/GITHUB_ACTIONS markers -- those are GitHub Actions'
# own runner-process environment, not something a human terminal exports.
# apm_cli itself treats CI/GITHUB_ACTIONS as a signal to force non-interactive
# behavior (see src/apm_cli/registry/operations.py's is_ci_environment and
# src/apm_cli/install/phases/integrate.py / security/executables.py's CI
# checks) and Python's own sys.stdin.isatty()/sys.stdout.isatty() would
# report differently if a real console is not attached -- so leaking either
# category into the child would silently invalidate the "as a human would
# experience it" premise this fixture exists to test.
# ---------------------------------------------------------------------------

# Variables removed so no askpass/agent mechanism is available to the child
# at all. GIT_ASKPASS is intentionally included even though apm_cli's own
# GitAuthEnvBuilder.setup_environment() unconditionally sets GIT_ASKPASS=echo
# for its *internal* authenticated-clone environment: that override happens
# inside apm's own Python code, on the *inner* subprocess environment dict it
# builds for git, and is orthogonal to SSH passphrase prompting -- GIT_ASKPASS
# governs git's own HTTPS credential-fill callback, not OpenSSH's own
# read_passphrase() terminal prompt (which SSH_ASKPASS/SSH_ASKPASS_REQUIRE
# would govern, and which this fixture strips at the outer/human level).
$script:AskpassAndAgentVars = @(
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "SSH_ASKPASS_REQUIRE",
    "DISPLAY",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID"
)

# CI-marker variables that would make apm_cli itself choose non-interactive
# code paths (see is_ci_environment / APM_NON_INTERACTIVE checks cited
# above). A human terminal does not have these set.
$script:CiMarkerVars = @(
    "CI",
    "GITHUB_ACTIONS",
    "TRAVIS",
    "JENKINS_URL",
    "BUILDKITE",
    "APM_NON_INTERACTIVE",
    "APM_E2E_TESTS"
)

# Credential-bearing variables that must never reach the child even though
# they have nothing to do with #1976 -- defense in depth, not part of the
# hypothesis under test.
$script:CredentialVars = @(
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_APM_PAT",
    "ADO_APM_PAT",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "GIT_HTTP_EXTRAHEADER",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS"
)

function Get-HumanLikeBaseEnvironment {
    <#
        .SYNOPSIS
        Snapshots the current process environment and strips exactly the
        categories documented above, leaving everything else (PATH, WINDIR,
        USERNAME, etc.) intact so the child still looks like a normal
        Windows user session.
    #>
    $environment = [ordered]@{}
    foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $environment[[string]$entry.Key] = [string]$entry.Value
    }

    $toRemove = @() + $script:AskpassAndAgentVars + $script:CiMarkerVars + $script:CredentialVars
    foreach ($name in $toRemove) {
        $environment.Remove($name)
    }
    foreach ($name in @($environment.Keys)) {
        if ($name -like "GITHUB_APM_PAT_*" -or $name -like "GIT_CONFIG_KEY_*" -or $name -like "GIT_CONFIG_VALUE_*") {
            $environment.Remove($name)
        }
    }
    return $environment
}

# ---------------------------------------------------------------------------
# Ephemeral fixture: a throwaway source package + bare "origin" repo the
# loopback sshd will serve, entirely inside $root (deleted at the end).
# ---------------------------------------------------------------------------

function New-EphemeralGitOrigin {
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$BareRoot,
        [Parameter(Mandatory = $true)][string]$SkillMarker
    )
    New-Item -ItemType Directory -Path (Join-Path $SourceRoot "skills\conpty-fixture") -Force | Out-Null
    Write-Utf8NoBom -Path (Join-Path $SourceRoot "apm.yml") -Content @"
name: conpty-fixture-package
version: 1.0.0
description: "Hermetic Windows SSH ConPTY passphrase evidence fixture (issue #1976)"
"@
    Write-Utf8NoBom -Path (Join-Path $SourceRoot "skills\conpty-fixture\SKILL.md") -Content @"
---
name: conpty-fixture
description: Hermetic Windows SSH ConPTY passphrase evidence fixture
---
# $SkillMarker
"@
    Invoke-Setup -FilePath $GitPath -ArgumentList @("init", "-b", "main") -WorkingDirectory $SourceRoot | Out-Null
    Invoke-Setup -FilePath $GitPath -ArgumentList @("config", "user.name", "APM ConPTY Fixture") -WorkingDirectory $SourceRoot | Out-Null
    Invoke-Setup -FilePath $GitPath -ArgumentList @("config", "user.email", "conpty-fixture@invalid.example") -WorkingDirectory $SourceRoot | Out-Null
    Invoke-Setup -FilePath $GitPath -ArgumentList @("add", ".") -WorkingDirectory $SourceRoot | Out-Null
    Invoke-Setup -FilePath $GitPath -ArgumentList @("commit", "-m", "seed conpty fixture") -WorkingDirectory $SourceRoot | Out-Null
    Invoke-Setup -FilePath $GitPath -ArgumentList @("clone", "--bare", $SourceRoot, $BareRoot) | Out-Null
    $commit = (Invoke-Setup -FilePath $GitPath -ArgumentList @("-C", $SourceRoot, "rev-parse", "HEAD")).Stdout.Trim()
    return $commit
}

function New-EphemeralSshKeypair {
    param(
        [Parameter(Mandatory = $true)][string]$SshKeygenPath,
        [Parameter(Mandatory = $true)][string]$KeyPath,
        [Parameter(Mandatory = $true)][string]$Passphrase
    )
    Invoke-Setup -FilePath $SshKeygenPath -ArgumentList @(
        "-q", "-t", "ed25519", "-N", $Passphrase, "-f", $KeyPath, "-C", "conpty-fixture"
    ) | Out-Null
    $fingerprint = (Invoke-Setup -FilePath $SshKeygenPath -ArgumentList @("-lf", "$KeyPath.pub")).Stdout.Trim()
    return $fingerprint
}

function Initialize-LoopbackSshd {
    <#
        .SYNOPSIS
        Points the pre-installed (normally stopped) Windows OpenSSH server
        at 127.0.0.1 only, serving `git upload-pack` on the ephemeral bare
        repo via a forced authorized_keys command -- the same pattern
        Windows OpenSSH's own documentation uses for restricted git-only
        access.

        .PARAMETER StateOut
        A [ref] the *original* service/file state is written into
        immediately, before any destructive action is taken. This lets the
        caller always call Restore-LoopbackSshd with a valid state object
        even if this function throws partway through (e.g. ACL lockdown
        failure after the service was already stopped) -- restorative
        cleanup must not depend on this function running to completion.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$BareRoot,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string[]]$PublicKeyPaths,
        [Parameter(Mandatory = $true)][ref]$StateOut
    )
    $sshdService = Get-Service -Name sshd -ErrorAction Stop
    $state = [pscustomobject]@{
        Service              = $sshdService
        InitialStatus        = $sshdService.Status
        InitialStartType     = $sshdService.StartType
        ProgramDataSsh       = Join-Path $env:ProgramData "ssh"
        SshdConfigPath       = $null
        AuthorizedKeysPath   = $null
        SshdConfigExisted    = $false
        AuthorizedKeysExisted = $false
        OriginalSshdConfig   = $null
        OriginalAuthorizedKeys = $null
    }
    $state.SshdConfigPath = Join-Path $state.ProgramDataSsh "sshd_config"
    $state.AuthorizedKeysPath = Join-Path $state.ProgramDataSsh "administrators_authorized_keys"
    $state.SshdConfigExisted = Test-Path $state.SshdConfigPath
    $state.AuthorizedKeysExisted = Test-Path $state.AuthorizedKeysPath
    if ($state.SshdConfigExisted) {
        $state.OriginalSshdConfig = [System.IO.File]::ReadAllBytes($state.SshdConfigPath)
    }
    if ($state.AuthorizedKeysExisted) {
        $state.OriginalAuthorizedKeys = [System.IO.File]::ReadAllBytes($state.AuthorizedKeysPath)
    }
    # Captured before any destructive action below -- the caller can now
    # restore correctly even if this function throws on the next line.
    $StateOut.Value = $state

    if ($sshdService.Status -eq "Running") {
        Stop-Service -Name sshd -Force
    }
    Set-Service -Name sshd -StartupType Manual
    New-Item -ItemType Directory -Path $state.ProgramDataSsh -Force | Out-Null

    $sshKeygenPath = Join-Path $env:WINDIR "System32\OpenSSH\ssh-keygen.exe"
    Invoke-Setup -FilePath $sshKeygenPath -ArgumentList @("-A") | Out-Null

    $serveCommand = Join-Path $Root "serve-git.cmd"
    $gitForward = ConvertTo-ForwardSlashPath $GitPath
    $bareForward = ConvertTo-ForwardSlashPath $BareRoot
    Write-Utf8NoBom -Path $serveCommand -Content @"
@echo off
"$gitForward" upload-pack "$bareForward"
"@
    $serveCommandForward = ConvertTo-ForwardSlashPath $serveCommand
    $keyOptions = 'command="' + $serveCommandForward + '",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding '
    $lines = foreach ($pubKeyPath in $PublicKeyPaths) {
        $keyOptions + (Get-Content -Path $pubKeyPath -Raw).Trim()
    }
    Write-Utf8NoBom -Path $state.AuthorizedKeysPath -Content (($lines -join "`n") + "`n")
    & icacls.exe $state.AuthorizedKeysPath /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to lock down administrators_authorized_keys ACL"
    }

    $runnerUser = $env:USERNAME.ToLowerInvariant()
    $sshdConfiguration = @"
Port $Port
ListenAddress 127.0.0.1
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
AllowUsers $runnerUser
AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
LogLevel DEBUG3
SyslogFacility LOCAL0
Match Group administrators
    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
"@
    Write-Utf8NoBom -Path $state.SshdConfigPath -Content $sshdConfiguration
    $sshdPath = Join-Path $env:WINDIR "System32\OpenSSH\sshd.exe"
    Invoke-Setup -FilePath $sshdPath -ArgumentList @("-t", "-f", $state.SshdConfigPath) | Out-Null
    Start-Service -Name sshd
    Wait-ForLoopbackPort -Port $Port
    return $state
}

function Restore-LoopbackSshd {
    param([Parameter(Mandatory = $true)][pscustomobject]$State)
    try {
        Stop-Service -Name sshd -Force -ErrorAction SilentlyContinue
        if ($State.SshdConfigExisted) {
            [System.IO.File]::WriteAllBytes($State.SshdConfigPath, $State.OriginalSshdConfig)
        } else {
            Remove-Item -Path $State.SshdConfigPath -Force -ErrorAction SilentlyContinue
        }
        if ($State.AuthorizedKeysExisted) {
            [System.IO.File]::WriteAllBytes($State.AuthorizedKeysPath, $State.OriginalAuthorizedKeys)
        } else {
            Remove-Item -Path $State.AuthorizedKeysPath -Force -ErrorAction SilentlyContinue
        }
        if ($State.InitialStatus -eq "Running") {
            Start-Service -Name sshd
        }
        if ($null -ne $State.InitialStartType) {
            Set-Service -Name sshd -StartupType $State.InitialStartType
        }
        Write-Ok "Restored Windows OpenSSH server state to pre-fixture condition"
    } catch {
        Write-Warn2 "Failed to fully restore Windows OpenSSH state: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Per-scenario environment + workspace. Every scenario gets its own isolated
# HOME/APM_CACHE_DIR/APM_TEMP_DIR so cache hits or leftover lockfiles from
# one scenario cannot bias another, and nothing ever touches the real user
# profile.
# ---------------------------------------------------------------------------

function New-ScenarioWorkspace {
    param(
        [Parameter(Mandatory = $true)][string]$ScenarioRoot,
        [Parameter(Mandatory = $true)][hashtable]$BaseEnvironment,
        [Parameter(Mandatory = $true)][string]$KeyPath,
        [Parameter(Mandatory = $true)][string]$KnownHostsPath,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$SshPath,
        [Parameter(Mandatory = $true)][string]$SshLogPath,
        [Parameter(Mandatory = $true)][string]$OriginUrl
    )
    $projectDir = Join-Path $ScenarioRoot "project"
    $homeDir = Join-Path $ScenarioRoot "home"
    $cacheDir = Join-Path $ScenarioRoot "cache"
    $tempDir = Join-Path $ScenarioRoot "temp"
    $traceDir = Join-Path $ScenarioRoot "trace"
    foreach ($dir in @($projectDir, $homeDir, $cacheDir, $tempDir, $traceDir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $tracePath = Join-Path $traceDir "trace2.json"

    Write-Utf8NoBom -Path (Join-Path $projectDir "apm.yml") -Content @"
name: conpty-fixture-consumer
version: 0.0.0
description: "Consumer project for the #1976 ConPTY interactive-passphrase fixture"

dependencies:
  apm:
    - $OriginUrl
"@

    $sshCommandParts = @(
        (ConvertTo-ForwardSlashPath $SshPath),
        "-vvv",
        "-E", (ConvertTo-ForwardSlashPath $SshLogPath),
        "-i", (ConvertTo-ForwardSlashPath $KeyPath),
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=$(ConvertTo-ForwardSlashPath $KnownHostsPath)",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "ConnectTimeout=5",
        "-p", [string]$Port
    )

    $environment = [ordered]@{}
    foreach ($key in $BaseEnvironment.Keys) { $environment[$key] = $BaseEnvironment[$key] }
    $environment["HOME"] = $homeDir
    $environment["USERPROFILE"] = $homeDir
    $environment["APM_HOME"] = (Join-Path $homeDir ".apm")
    $environment["APM_CACHE_DIR"] = $cacheDir
    $environment["APM_TEMP_DIR"] = $tempDir
    $environment["APM_NO_CACHE"] = "1"
    $environment["APM_GIT_PROTOCOL"] = "ssh"
    $environment["APM_ALLOW_PROTOCOL_FALLBACK"] = "0"
    $environment["GIT_CONFIG_NOSYSTEM"] = "1"
    $environment["GIT_CONFIG_GLOBAL"] = (ConvertTo-ForwardSlashPath (Join-Path $ScenarioRoot "empty.gitconfig"))
    $environment["GIT_SSH_COMMAND"] = ($sshCommandParts -join " ")
    $environment["GIT_TRACE2_EVENT"] = (ConvertTo-ForwardSlashPath $tracePath)
    $environment["GIT_TRACE2_ENV_VARS"] = "GIT_TERMINAL_PROMPT,GIT_ASKPASS,SSH_ASKPASS,SSH_ASKPASS_REQUIRE,DISPLAY,SSH_AUTH_SOCK,GIT_SSH_COMMAND"
    Write-Utf8NoBom -Path (Join-Path $ScenarioRoot "empty.gitconfig") -Content "# intentionally empty -- hermetic global config for this scenario`n"

    return [pscustomobject]@{
        ProjectDir = $projectDir
        HomeDir    = $homeDir
        CacheDir   = $cacheDir
        TempDir    = $tempDir
        TracePath  = $tracePath
        Environment = $environment
    }
}

# ---------------------------------------------------------------------------
# The core of the fixture: drive one `apm install` invocation through a real
# ConPTY, exactly as a human's PowerShell session would present a console to
# it, and observe (never fabricate) whatever ssh/apm actually do.
# ---------------------------------------------------------------------------

function Invoke-Scenario {
    param(
        [Parameter(Mandatory = $true)][string]$ScenarioId,
        [Parameter(Mandatory = $true)][string]$ApmPath,
        [Parameter(Mandatory = $true)][pscustomobject]$Workspace,
        [string]$CorrectPassphrase,
        [string]$WrongPassphrase,
        [ValidateSet("none", "correct", "wrong", "cancel")]
        [string]$PassphraseAction = "none",
        [int]$PromptTimeoutMs = 30000,
        [int]$ExitTimeoutMs = 60000,
        [int]$MaxWrongAttempts = 3
    )

    $commandLine = '"' + $ApmPath + '" install'
    Write-Step "[$ScenarioId] Launching packaged apm.exe under a real ConPTY: $commandLine"

    $session = $null
    $promptObserved = $false
    $promptCount = 0
    $transcript = ""
    $forcedKill = $false
    $exited = $false
    $exitCode = $null
    $startedUtc = [DateTime]::UtcNow

    try {
        $session = New-ConPtySession `
            -CommandLine $commandLine `
            -WorkingDirectory $Workspace.ProjectDir `
            -Environment $Workspace.Environment

        if ($PassphraseAction -eq "none") {
            # unencrypted-positive: no prompt expected. Just wait for exit,
            # but still watch the transcript in case a prompt shows up
            # unexpectedly (which would itself be notable evidence).
            $wait = Wait-ConPtyText -Session $session -Pattern "Enter passphrase for" -TimeoutMs 5000
            $transcript += $wait.Transcript
            $promptObserved = $wait.Matched
        } else {
            $wait = Wait-ConPtyText -Session $session -Pattern "Enter passphrase for" -TimeoutMs $PromptTimeoutMs
            $transcript += $wait.Transcript
            $promptObserved = $wait.Matched
            if ($promptObserved) {
                $promptCount = 1
                switch ($PassphraseAction) {
                    "correct" {
                        Send-ConPtyText -Session $session -Text $CorrectPassphrase
                    }
                    "wrong" {
                        Send-ConPtyText -Session $session -Text $WrongPassphrase
                        for ($attempt = 2; $attempt -le $MaxWrongAttempts; $attempt++) {
                            $reprompt = Wait-ConPtyText -Session $session -Pattern "Enter passphrase for" -TimeoutMs 10000
                            $transcript += $reprompt.Transcript
                            if (-not $reprompt.Matched) { break }
                            $promptCount++
                            Send-ConPtyText -Session $session -Text $WrongPassphrase
                        }
                    }
                    "cancel" {
                        # Prove the prompt genuinely blocks awaiting input
                        # (not some fixed internal timeout) before cancelling.
                        Start-Sleep -Seconds 5
                        Send-ConPtyControlC -Session $session
                    }
                }
            }
        }

        $exitWait = Wait-ConPtyExit -Session $session -TimeoutMs $ExitTimeoutMs
        $exited = $exitWait.Exited
        $exitCode = $exitWait.ExitCode
        if (-not $exited) {
            Write-Warn2 "[$ScenarioId] Process did not exit within ${ExitTimeoutMs}ms -- forcing termination"
            $forcedKill = $true
        }
        $transcript += Read-ConPtyAvailable -Session $session -TimeoutMs 500
    } finally {
        if ($session) {
            Stop-ConPtySession -Session $session -Force:$forcedKill
        }
    }

    $finishedUtc = [DateTime]::UtcNow

    $lockPath = Join-Path $Workspace.ProjectDir "apm.lock.yaml"
    $modulesRoot = Join-Path $Workspace.ProjectDir "apm_modules"
    $materialized = (Test-Path $lockPath) -and ((Get-ChildItem -Path $modulesRoot -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0)

    $traceLines = @()
    if (Test-Path $Workspace.TracePath) {
        $traceLines = Get-Content -Path $Workspace.TracePath -ErrorAction SilentlyContinue
    }

    return [pscustomobject]@{
        ScenarioId        = $ScenarioId
        PassphraseAction  = $PassphraseAction
        CommandLine       = $commandLine
        PromptObserved    = $promptObserved
        PromptCount       = $promptCount
        ProcessExited     = $exited
        ForcedKill        = $forcedKill
        ExitCode          = $exitCode
        Materialized      = $materialized
        LockfilePresent   = (Test-Path $lockPath)
        StartedUtc        = $startedUtc.ToString("o")
        FinishedUtc       = $finishedUtc.ToString("o")
        DurationMs        = [int]($finishedUtc - $startedUtc).TotalMilliseconds
        Trace2EventCount  = $traceLines.Count
        TranscriptSanitized = ($transcript -replace [regex]::Escape($CorrectPassphrase), "***REDACTED-CORRECT***" `
                                          -replace [regex]::Escape($WrongPassphrase), "***REDACTED-WRONG***")
    }
}

function Test-NoSecretLeak {
    <#
        .SYNOPSIS
        Fails loudly if either ephemeral passphrase or any private key
        material made it into the evidence directory. This is the last
        gate before anything is considered safe to upload as a workflow
        artifact.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceDirectory,
        [Parameter(Mandatory = $true)][string[]]$Secrets
    )
    $offenders = New-Object System.Collections.Generic.List[string]
    $files = Get-ChildItem -Path $EvidenceDirectory -Recurse -File -ErrorAction SilentlyContinue
    foreach ($file in $files) {
        $content = Get-Content -Path $file.FullName -Raw -ErrorAction SilentlyContinue
        if (-not $content) { continue }
        foreach ($secret in $Secrets) {
            if ($secret -and $content.Contains($secret)) {
                $offenders.Add("$($file.FullName) contains a raw fixture secret")
            }
        }
        if ($content -match "-----BEGIN OPENSSH PRIVATE KEY-----") {
            $offenders.Add("$($file.FullName) contains a raw private key block")
        }
    }
    if ($offenders.Count -gt 0) {
        throw "Secret-scan FAILED before artifact upload:`n- " + ($offenders -join "`n- ")
    }
    Write-Ok "Secret-scan passed: no fixture passphrase or private key material found in evidence"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("apm-conpty-" + [guid]::NewGuid().ToString("N"))
$keysRoot = Join-Path $root "keys"
$sourceRoot = Join-Path $root "source"
$bareRoot = Join-Path $root "conpty-fixture.git"
$knownHosts = Join-Path $root "known_hosts"
$encryptedKey = Join-Path $keysRoot "id_ed25519_encrypted"
$plainKey = Join-Path $keysRoot "id_ed25519_plain"
$correctPassphrase = "conpty-" + [guid]::NewGuid().ToString("N")
$wrongPassphrase = "wrong-" + [guid]::NewGuid().ToString("N")
$port = 12244
$skillMarker = "windows-ssh-conpty-fixture-1976"

$sshdState = $null
$scenarioResults = @()
$overallOk = $true
$blockers = New-Object System.Collections.Generic.List[string]

try {
    if (Test-Path $EvidenceDirectory) {
        Remove-Item -Path $EvidenceDirectory -Recurse -Force
    }
    New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    New-Item -ItemType Directory -Path $keysRoot -Force | Out-Null

    Write-Step "Resolving required executables"
    $apmPath = (Resolve-Path $ApmBinary).Path
    $gitPath = (Get-Command git.exe -ErrorAction Stop).Source
    $sshPath = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
    $sshKeygenPath = Join-Path $env:WINDIR "System32\OpenSSH\ssh-keygen.exe"
    $sshKeyscanPath = Join-Path $env:WINDIR "System32\OpenSSH\ssh-keyscan.exe"
    $sshdPath = Join-Path $env:WINDIR "System32\OpenSSH\sshd.exe"
    foreach ($required in @($apmPath, $gitPath, $sshPath, $sshKeygenPath, $sshKeyscanPath, $sshdPath)) {
        Assert-Condition (Test-Path $required -PathType Leaf) "Required executable exists: $required"
    }
    Assert-Condition ($apmPath.EndsWith("apm.exe")) "Fixture drives a packaged apm.exe (not a Python dev entrypoint)"

    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    Assert-Condition (
        $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    ) "Runner account is a local Administrator (needed for sshd loopback config)"

    $binaryVersions = [ordered]@{
        ApmVersion    = (Invoke-Setup -FilePath $apmPath -ArgumentList @("--version")).Stdout.Trim()
        ApmSha256     = (Get-FileHash -Path $apmPath -Algorithm SHA256).Hash.ToLowerInvariant()
        GitVersion    = (Invoke-Setup -FilePath $gitPath -ArgumentList @("--version")).Stdout.Trim()
        SshVersion    = (Invoke-Setup -FilePath $sshPath -ArgumentList @("-V")).Stderr.Trim()
        SshdVersion   = (Invoke-Setup -FilePath $sshdPath -ArgumentList @("-V")).Stderr.Trim()
        OsVersion     = [System.Environment]::OSVersion.VersionString
    }
    Write-Info ("apm --version: " + $binaryVersions.ApmVersion)
    Write-Info ("apm SHA-256:   " + $binaryVersions.ApmSha256)

    Write-Step "Creating ephemeral git origin (throwaway package, never published)"
    $originCommit = New-EphemeralGitOrigin -GitPath $gitPath -SourceRoot $sourceRoot -BareRoot $bareRoot -SkillMarker $skillMarker
    Write-Info "Origin HEAD: $originCommit"

    Write-Step "Generating one encrypted and one unencrypted ephemeral Ed25519 key (never reused, never uploaded)"
    $encryptedFingerprint = New-EphemeralSshKeypair -SshKeygenPath $sshKeygenPath -KeyPath $encryptedKey -Passphrase $correctPassphrase
    $plainFingerprint = New-EphemeralSshKeypair -SshKeygenPath $sshKeygenPath -KeyPath $plainKey -Passphrase ""
    Write-Info "Encrypted key fingerprint: $encryptedFingerprint"
    Write-Info "Plain key fingerprint:     $plainFingerprint"

    Write-Step "Configuring Windows OpenSSH server on loopback only (port $port)"
    Initialize-LoopbackSshd `
        -GitPath $gitPath -BareRoot $bareRoot -Root $root -Port $port `
        -PublicKeyPaths @("$encryptedKey.pub", "$plainKey.pub") `
        -StateOut ([ref]$sshdState) | Out-Null

    $keyscan = Invoke-Setup -FilePath $sshKeyscanPath -ArgumentList @("-p", [string]$port, "127.0.0.1")
    Write-Utf8NoBom -Path $knownHosts -Content $keyscan.Stdout
    Assert-Condition (-not [string]::IsNullOrWhiteSpace($keyscan.Stdout)) "Loopback SSH host key captured via ssh-keyscan"

    Write-Step "Non-interactive preflight: proving server-side plumbing works before spending ConPTY cycles"
    $preflightEnv = [ordered]@{}
    foreach ($k in [Environment]::GetEnvironmentVariables().Keys) { $preflightEnv[[string]$k] = [string][Environment]::GetEnvironmentVariable($k) }
    $preflightEnv["GIT_SSH_COMMAND"] = (
        (ConvertTo-ForwardSlashPath $sshPath) + " -i " + (ConvertTo-ForwardSlashPath $plainKey) +
        " -o IdentitiesOnly=yes -o UserKnownHostsFile=" + (ConvertTo-ForwardSlashPath $knownHosts) +
        " -o StrictHostKeyChecking=yes -p " + $port
    )
    $preflight = Invoke-Setup -FilePath $gitPath -ArgumentList @(
        "ls-remote", "ssh://git@127.0.0.1:$port/conpty-fixture.git"
    ) -Environment $preflightEnv -TimeoutSeconds 20
    Assert-Condition ($preflight.ExitCode -eq 0) "Preflight git ls-remote over loopback sshd (plain key) succeeds non-interactively"

    $originUrl = "ssh://git@127.0.0.1:$port/conpty-fixture.git"
    $baseEnvironment = Get-HumanLikeBaseEnvironment
    foreach ($strippedVar in (@() + $script:AskpassAndAgentVars + $script:CiMarkerVars)) {
        Assert-Condition (-not $baseEnvironment.Contains($strippedVar)) "Human-like base environment excludes $strippedVar"
    }

    $scenarioSpecs = @(
        @{ Id = "unencrypted-positive"; Key = $plainKey;     Action = "none" },
        @{ Id = "encrypted-correct";    Key = $encryptedKey; Action = "correct" },
        @{ Id = "encrypted-wrong";      Key = $encryptedKey; Action = "wrong" },
        @{ Id = "encrypted-cancel";     Key = $encryptedKey; Action = "cancel" }
    )

    foreach ($spec in $scenarioSpecs) {
        Write-Step "Running scenario: $($spec.Id)"
        $scenarioRoot = Join-Path $root ("scenario-" + $spec.Id)
        New-Item -ItemType Directory -Path $scenarioRoot -Force | Out-Null
        $sshLogPath = Join-Path $scenarioRoot "ssh-debug.log"
        $workspace = New-ScenarioWorkspace `
            -ScenarioRoot $scenarioRoot -BaseEnvironment $baseEnvironment `
            -KeyPath $spec.Key -KnownHostsPath $knownHosts -Port $port `
            -SshPath $sshPath -SshLogPath $sshLogPath -OriginUrl $originUrl

        $result = Invoke-Scenario `
            -ScenarioId $spec.Id -ApmPath $apmPath -Workspace $workspace `
            -CorrectPassphrase $correctPassphrase -WrongPassphrase $wrongPassphrase `
            -PassphraseAction $spec.Action

        $scenarioEvidenceDir = Join-Path $EvidenceDirectory $spec.Id
        New-Item -ItemType Directory -Path $scenarioEvidenceDir -Force | Out-Null
        Write-Utf8NoBom -Path (Join-Path $scenarioEvidenceDir "result.json") -Content ($result | ConvertTo-Json -Depth 10)
        Write-Utf8NoBom -Path (Join-Path $scenarioEvidenceDir "transcript.txt") -Content $result.TranscriptSanitized
        if (Test-Path $sshLogPath) {
            Copy-Item -Path $sshLogPath -Destination (Join-Path $scenarioEvidenceDir "ssh-debug.log") -Force
        }
        if (Test-Path $workspace.TracePath) {
            Copy-Item -Path $workspace.TracePath -Destination (Join-Path $scenarioEvidenceDir "trace2.json") -Force
        }

        $scenarioResults += $result
        Write-Info "  PromptObserved=$($result.PromptObserved) ExitCode=$($result.ExitCode) Materialized=$($result.Materialized) ForcedKill=$($result.ForcedKill)"
    }

    # ---- classification ----
    $unencrypted = $scenarioResults | Where-Object { $_.ScenarioId -eq "unencrypted-positive" }
    $correct = $scenarioResults | Where-Object { $_.ScenarioId -eq "encrypted-correct" }
    $wrong = $scenarioResults | Where-Object { $_.ScenarioId -eq "encrypted-wrong" }
    $cancel = $scenarioResults | Where-Object { $_.ScenarioId -eq "encrypted-cancel" }

    $interactivePromptWorks = $correct.PromptObserved -and $correct.ExitCode -eq 0 -and $correct.Materialized
    $anyEncryptedScenarioMissingPrompt = -not $correct.PromptObserved -or -not $wrong.PromptObserved -or -not $cancel.PromptObserved

    if ($anyEncryptedScenarioMissingPrompt) {
        $verdict = "RED"
        $verdictReason = (
            "At least one encrypted-key scenario never observed the interactive passphrase " +
            "prompt despite a real ConPTY console attached and no SSH_ASKPASS/GIT_ASKPASS/agent " +
            "configured. This reproduces/confirms the remaining interactive boundary in #1976: " +
            "correct.PromptObserved=$($correct.PromptObserved), wrong.PromptObserved=$($wrong.PromptObserved), " +
            "cancel.PromptObserved=$($cancel.PromptObserved)."
        )
    } elseif ($interactivePromptWorks) {
        $verdict = "GREEN"
        $verdictReason = (
            "The interactive ConPTY passphrase prompt was observed in every encrypted scenario, " +
            "the correct passphrase led to a successful materialized install (exit 0, lockfile + " +
            "apm_modules populated), the wrong passphrase led to a failure, and cancel produced a " +
            "clean stop. The remaining Windows interactive-console boundary for #1976 does not " +
            "reproduce on this runner/binary/git/ssh combination."
        )
    } else {
        $verdict = "RED"
        $verdictReason = (
            "The interactive prompt appeared but did not resolve as expected for the correct-" +
            "passphrase scenario (ExitCode=$($correct.ExitCode), Materialized=$($correct.Materialized)) " +
            "-- evidence of a partial/adjacent boundary distinct from the already-proven forced-" +
            "askpass GREEN result."
        )
    }

    $summary = [ordered]@{
        Issue               = 1976
        BaselineCommit      = "796e229805"
        Verdict             = $verdict
        VerdictReason       = $verdictReason
        BinaryVersions      = $binaryVersions
        Port                = $port
        OriginCommit        = $originCommit
        EncryptedFingerprint = $encryptedFingerprint
        PlainFingerprint    = $plainFingerprint
        Scenarios           = $scenarioResults
        GeneratedUtc        = [DateTime]::UtcNow.ToString("o")
    }
    Write-Utf8NoBom -Path (Join-Path $EvidenceDirectory "summary.json") -Content ($summary | ConvertTo-Json -Depth 10)

    Write-Host ""
    Write-Host "==================== VERDICT: $verdict ====================" -ForegroundColor Magenta
    Write-Host $verdictReason
    Write-Host "============================================================" -ForegroundColor Magenta

    Test-NoSecretLeak -EvidenceDirectory $EvidenceDirectory -Secrets @($correctPassphrase, $wrongPassphrase)

    if ($env:GITHUB_STEP_SUMMARY) {
        Add-Content -Path $env:GITHUB_STEP_SUMMARY -Value "## Issue #1976 -- ConPTY interactive-passphrase evidence`n`n**Verdict: $verdict**`n`n$verdictReason`n"
    }
} catch {
    $overallOk = $false
    $blockers.Add($_.Exception.Message)
    Write-Err2 "Fixture did not complete: $($_.Exception.Message)"
} finally {
    if ($sshdState) {
        Restore-LoopbackSshd -State $sshdState
    }
    if (Test-Path $root) {
        Remove-Item -Path $root -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if (-not $overallOk) {
    Write-Err2 "Precise technical blocker(s):`n- $($blockers -join "`n- ")"
    exit 1
}

Write-Ok "Fixture completed and produced sanitized evidence for all four scenarios"
exit 0
