# Thin PowerShell wrapper around ApmConPty.ConPtyProcess (ConPtyNative.cs).
#
# This module exists to keep the #1976 interactive-evidence orchestrator
# (test-ssh-conpty-passphrase.ps1) readable: all the raw kernel32/ConPTY
# plumbing lives in ConPtyNative.cs, and this file only exposes small,
# reviewable verbs a scenario runner can call in sequence.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:ConPtyTypeLoaded = $false

function Import-ConPtyNativeType {
    <#
        .SYNOPSIS
        Compiles ConPtyNative.cs into the current session exactly once.
    #>
    if ($script:ConPtyTypeLoaded) {
        return
    }
    $csPath = Join-Path $PSScriptRoot "ConPtyNative.cs"
    if (-not (Test-Path $csPath)) {
        throw "ConPtyNative.cs not found next to ConPtySession.psm1 at: $csPath"
    }
    $source = Get-Content -Raw -Path $csPath
    Add-Type -TypeDefinition $source -Language CSharp
    $script:ConPtyTypeLoaded = $true
}

function New-ConPtySession {
    <#
        .SYNOPSIS
        Starts a child process attached to a brand-new pseudo console.

        .PARAMETER CommandLine
        The full Win32 command line (already quoted), e.g. '"C:\apm.exe" install'.

        .PARAMETER WorkingDirectory
        Directory the child process starts in.

        .PARAMETER Environment
        Hashtable of the *exact* environment block the child receives. No
        values are inherited implicitly: build this explicitly so the
        fixture can prove precisely which variables were present or absent
        (e.g. no SSH_ASKPASS/GIT_ASKPASS/DISPLAY).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][hashtable]$Environment,
        [int]$Columns = 120,
        [int]$Rows = 32
    )
    Import-ConPtyNativeType
    return [ApmConPty.ConPtyProcess]::Start(
        $CommandLine,
        $WorkingDirectory,
        $Environment,
        [int16]$Columns,
        [int16]$Rows)
}

function Read-ConPtyAvailable {
    param(
        [Parameter(Mandatory = $true)][ApmConPty.ConPtyProcess]$Session,
        [int]$TimeoutMs = 200
    )
    return $Session.ReadAvailable($TimeoutMs)
}

function Wait-ConPtyText {
    <#
        .SYNOPSIS
        Polls console output until a regex matches or the timeout elapses.

        .OUTPUTS
        A hashtable with Matched (bool) and Transcript (string, full text
        observed while waiting -- used both to detect the prompt and as
        evidence of exactly what the interactive session displayed).
    #>
    param(
        [Parameter(Mandatory = $true)][ApmConPty.ConPtyProcess]$Session,
        [Parameter(Mandatory = $true)][string]$Pattern,
        [int]$TimeoutMs = 20000
    )
    $regex = [System.Text.RegularExpressions.Regex]::new($Pattern)
    $isMatch = [Func[string, bool]] { param($text) $regex.IsMatch($text) }
    $result = $Session.WaitForText($isMatch, $TimeoutMs, 200)
    return @{
        Matched    = $result.Item1
        Transcript = $result.Item2
    }
}

function Send-ConPtyText {
    <#
        .SYNOPSIS
        Types text into the console input followed by Enter, as a human
        would type a passphrase and press return.
    #>
    param(
        [Parameter(Mandatory = $true)][ApmConPty.ConPtyProcess]$Session,
        [Parameter(Mandatory = $true)][string]$Text
    )
    $Session.WriteInput($Text + "`r")
}

function Send-ConPtyControlC {
    <#
        .SYNOPSIS
        Sends a raw ETX (0x03) byte -- the same signal a real console
        delivers to the attached process when a human presses Ctrl+C.
    #>
    param(
        [Parameter(Mandatory = $true)][ApmConPty.ConPtyProcess]$Session
    )
    $Session.WriteRawByte(3)
}

function Wait-ConPtyExit {
    param(
        [Parameter(Mandatory = $true)][ApmConPty.ConPtyProcess]$Session,
        [int]$TimeoutMs = 15000
    )
    $exitCode = 0
    $exited = $Session.WaitForExit($TimeoutMs, [ref]$exitCode)
    return @{
        Exited   = $exited
        ExitCode = $exitCode
    }
}

function Stop-ConPtySession {
    param(
        [Parameter(Mandatory = $true)][ApmConPty.ConPtyProcess]$Session,
        [switch]$Force
    )
    if ($Force) {
        try { $Session.Kill() } catch { Write-Verbose "Kill failed (process likely already exited): $_" }
    }
    $Session.Dispose()
}

Export-ModuleMember -Function @(
    "Import-ConPtyNativeType",
    "New-ConPtySession",
    "Read-ConPtyAvailable",
    "Wait-ConPtyText",
    "Send-ConPtyText",
    "Send-ConPtyControlC",
    "Wait-ConPtyExit",
    "Stop-ConPtySession"
)
