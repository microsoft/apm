# Build APM binary for Windows using PyInstaller
# PowerShell equivalent of build-binary.sh

$ErrorActionPreference = "Stop"

# Platform detection
$arch = [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture
switch ($arch) {
    "X64"   { $Arch = "x86_64" }
    "Arm64" { $Arch = "x86_64" }  # x86_64 emulation on ARM64
    default {
        Write-Host "Unsupported architecture: $arch" -ForegroundColor Red
        exit 1
    }
}

$BinaryName = "apm-windows-$Arch"

Write-Host "Building APM binary for windows-$Arch" -ForegroundColor Blue
Write-Host "Output binary: $BinaryName" -ForegroundColor Blue

# Clean previous builds
Write-Host "Cleaning previous builds..." -ForegroundColor Yellow
if (Test-Path "build/build") { Remove-Item -Recurse -Force "build/build" }
if (Test-Path "dist")        { Remove-Item -Recurse -Force "dist" }

# Check if PyInstaller is available
try {
    uv run pyinstaller --version | Out-Null
} catch {
    Write-Host "PyInstaller not found. Make sure dependencies are installed with: uv sync --extra build" -ForegroundColor Red
    exit 1
}

# Check if UPX is available (optional)
if (Get-Command upx -ErrorAction SilentlyContinue) {
    Write-Host "UPX found - binary will be compressed" -ForegroundColor Green
} else {
    Write-Host "UPX not found - binary will not be compressed" -ForegroundColor Yellow
}

# Inject build SHA into version.py
$VersionFile = "src/apm_cli/version.py"
$originalContent = Get-Content $VersionFile -Raw
$BuildSHA = git rev-parse --short HEAD 2>$null
if ($BuildSHA) {
    Write-Host "Injecting build SHA: $BuildSHA" -ForegroundColor Yellow
    $newContent = $originalContent -replace '^__BUILD_SHA__ = None$', "__BUILD_SHA__ = `"$BuildSHA`""
    Set-Content -Path $VersionFile -Value $newContent -NoNewline
}

try {
    # Build binary
    Write-Host "Building binary with PyInstaller..." -ForegroundColor Yellow
    uv run pyinstaller build/apm.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    # Check if build was successful (onedir mode creates dist/apm/apm.exe)
    if (-not (Test-Path "dist/apm/apm.exe")) {
        Write-Host "Build failed - binary not found" -ForegroundColor Red
        exit 1
    }

    # Rename the directory to have the platform-specific name
    Rename-Item "dist/apm" $BinaryName

    # Authenticode-sign the binary when signing credentials are available.
    # Signing is optional: builds without WINDOWS_CERT_PFX succeed unsigned.
    if ($env:WINDOWS_CERT_PFX) {
        Write-Host "[*] Signing binary (Authenticode)..."
        $env:BINARY_DIR = "dist/$BinaryName"
        & pwsh scripts/windows/sign-binary.ps1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[x] Signing failed with exit code $LASTEXITCODE"
            exit $LASTEXITCODE
        }
    } else {
        Write-Host "[i] WINDOWS_CERT_PFX not set -- skipping Authenticode signing"
    }

    # Test the binary (temporarily relax error preference so stderr from native
    # commands does not throw under $ErrorActionPreference = "Stop")
    Write-Host "Testing binary..." -ForegroundColor Yellow
    $savedPref = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & "dist/$BinaryName/apm.exe" --version
    $testExit = $LASTEXITCODE
    $ErrorActionPreference = $savedPref
    if ($testExit -eq 0) {
        Write-Host "Binary test successful" -ForegroundColor Green
    } else {
        Write-Host "Binary test failed with exit code $testExit" -ForegroundColor Red
        exit 1
    }

    # Show binary info
    Write-Host "Build complete!" -ForegroundColor Green
    $size = (Get-ChildItem "dist/$BinaryName" -Recurse | Measure-Object -Property Length -Sum).Sum
    $sizeMB = [math]::Round($size / 1MB, 1)
    Write-Host "Binary: dist/$BinaryName/apm.exe" -ForegroundColor Blue
    Write-Host "Size: ${sizeMB}MB" -ForegroundColor Blue

    # Create checksum
    $hash = (Get-FileHash "dist/$BinaryName/apm.exe" -Algorithm SHA256).Hash.ToLower()
    "$hash  dist/$BinaryName/apm.exe" | Set-Content "dist/$BinaryName.sha256"
    Write-Host "Checksum: dist/$BinaryName.sha256" -ForegroundColor Blue

    Write-Host "Ready for release!" -ForegroundColor Green
} finally {
    # Restore version.py
    if ($BuildSHA) {
        Set-Content -Path $VersionFile -Value $originalContent -NoNewline
    }
}
