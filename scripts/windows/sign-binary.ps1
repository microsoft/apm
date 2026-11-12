# Authenticode-sign the APM Windows binary directory.
#
# Prerequisites (all mandatory for signing to proceed):
#   $env:WINDOWS_CERT_PFX       -- base64-encoded PFX certificate bundle
#   $env:WINDOWS_CERT_PASSWORD  -- PFX private-key password
#   $env:BINARY_DIR             -- path to the onedir bundle (default: dist\apm-windows-x86_64)
#
# If WINDOWS_CERT_PFX is absent or empty the script exits 0 without signing;
# this lets contributor / fork builds complete normally.
#
# Signing covers:
#   1. apm.exe (the main entry point)
#   2. Every bundled *.dll (unsigned DLLs can re-trigger Defender heuristics
#      even when the EXE itself is signed)
#
# A DigiCert RFC 3161 timestamp is embedded so signatures remain valid after
# certificate expiry.

$ErrorActionPreference = "Stop"

# -- Guard: skip gracefully when no certificate is configured ---------------

if (-not $env:WINDOWS_CERT_PFX) {
    Write-Host "[i] WINDOWS_CERT_PFX not set -- skipping Authenticode signing"
    exit 0
}

if (-not $env:WINDOWS_CERT_PASSWORD) {
    Write-Host "[x] WINDOWS_CERT_PFX is set but WINDOWS_CERT_PASSWORD is missing"
    exit 1
}

# -- Locate binary directory -------------------------------------------------

$BinaryDir = if ($env:BINARY_DIR) { $env:BINARY_DIR } else { "dist\apm-windows-x86_64" }

if (-not (Test-Path $BinaryDir)) {
    Write-Host "[x] Binary directory not found: $BinaryDir"
    exit 1
}

$ExePath = Join-Path $BinaryDir "apm.exe"
if (-not (Test-Path $ExePath)) {
    Write-Host "[x] apm.exe not found in $BinaryDir"
    exit 1
}

# -- Locate signtool.exe ------------------------------------------------------

# Windows SDK ships signtool.exe; GitHub runners have it on PATH via the
# Visual Studio component. Fall back to the well-known SDK x64 path.
$SignTool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
if (-not $SignTool) {
    $FallbackPaths = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin\x64\signtool.exe",
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe"
    )
    foreach ($p in $FallbackPaths) {
        if (Test-Path $p) { $SignTool = Get-Item $p; break }
    }
}
if (-not $SignTool) {
    Write-Host "[x] signtool.exe not found -- install Windows SDK or ensure it is on PATH"
    exit 1
}

$SignToolPath = if ($SignTool -is [System.Management.Automation.ApplicationInfo]) {
    $SignTool.Source
} else {
    $SignTool.FullName
}
Write-Host "[i] Using signtool: $SignToolPath"

# -- Extract PFX to a temp file ----------------------------------------------

$TempCert = $null
try {
    $TempCert = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::GetRandomFileName() + ".pfx")
    $certBytes = [System.Convert]::FromBase64String($env:WINDOWS_CERT_PFX)

    # Write PFX with exclusive lock (FileShare.None) so no other process can
    # open the file during the write window.
    $fs = [System.IO.File]::Open(
        $TempCert,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try { $fs.Write($certBytes, 0, $certBytes.Length) }
    finally { $fs.Dispose() }

    # Restrict ACL to current user only: disable ACE inheritance and remove
    # any inherited rules, then grant FullControl to this process's identity.
    $acl = Get-Acl -Path $TempCert
    $acl.SetAccessRuleProtection($true, $false)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
        'FullControl',
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl.SetAccessRule($rule)
    Set-Acl -Path $TempCert -AclObject $acl

    Write-Host "[i] Certificate decoded to temporary path (owner-only ACL applied)"

    # -- Collect targets: apm.exe + all bundled DLLs -------------------------

    $Targets = @($ExePath)
    $DllTargets = Get-ChildItem -Path $BinaryDir -Filter "*.dll" -Recurse |
        Select-Object -ExpandProperty FullName
    $Targets += $DllTargets

    Write-Host "[*] Signing $($Targets.Count) file(s)..."

    # -- Sign all targets in a single signtool invocation --------------------
    # Flags:
    #   /fd SHA256      -- file-digest algorithm
    #   /td SHA256      -- timestamp-digest algorithm
    #   /tr <url>       -- RFC 3161 timestamp server
    #   /f  <pfx>       -- certificate file
    #   /p  <password>  -- private-key password
    #   /q              -- quiet (only errors)

    $SignArgs = @(
        "sign",
        "/fd", "SHA256",
        "/td", "SHA256",
        "/tr", "https://timestamp.digicert.com",
        "/f", $TempCert,
        "/p", $env:WINDOWS_CERT_PASSWORD,
        "/q"
    ) + $Targets

    $proc = Start-Process -FilePath $SignToolPath -ArgumentList $SignArgs `
        -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -ne 0) {
        Write-Host "[x] signtool sign failed with exit code $($proc.ExitCode)"
        exit $proc.ExitCode
    }

    Write-Host "[+] Signing complete"

    # -- Verify the EXE signature --------------------------------------------

    Write-Host "[*] Verifying signature on apm.exe..."
    $VerifyArgs = @("verify", "/pa", "/v", $ExePath)
    $verifyProc = Start-Process -FilePath $SignToolPath -ArgumentList $VerifyArgs `
        -Wait -PassThru -NoNewWindow
    if ($verifyProc.ExitCode -ne 0) {
        Write-Host "[x] signtool verify failed -- signature may be invalid"
        exit $verifyProc.ExitCode
    }

    Write-Host "[+] Signature verified"

} finally {
    if ($TempCert -and (Test-Path $TempCert)) {
        Remove-Item -Force $TempCert -ErrorAction SilentlyContinue
        Write-Host "[i] Temporary certificate file removed"
    }
}
