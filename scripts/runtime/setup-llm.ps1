# Setup script for LLM runtime (Windows)
# Installs Simon Willison's llm library via pip in a managed environment

param(
    [switch]$Vanilla
)

$ErrorActionPreference = "Stop"

# Source common utilities
. "$PSScriptRoot\setup-common.ps1"

function Install-Llm {
    Write-Info "Setting up LLM runtime..."

    Initialize-ApmRuntimeDir

    $runtimeDir = Join-Path (Join-Path $env:USERPROFILE ".apm") "runtimes"
    $llmVenv = Join-Path $runtimeDir "llm-venv"
    $llmWrapper = Join-Path $runtimeDir "llm.cmd"

    # Check Python availability (on Windows it's 'python' not 'python3')
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Write-ErrorText "Python is required but not found. Please install Python 3."
        exit 1
    }

    # Create virtual environment
    Write-Info "Creating Python virtual environment for LLM..."
    python -m venv $llmVenv

    $pipExe = Join-Path (Join-Path $llmVenv "Scripts") "pip.exe"
    $llmExe = Join-Path (Join-Path $llmVenv "Scripts") "llm.exe"

    # Install LLM
    Write-Info "Installing LLM library..."
    & $pipExe install --upgrade pip
    & $pipExe install llm

    # Install GitHub Models plugin in non-vanilla mode
    if (-not $Vanilla) {
        Write-Info "Installing GitHub Models plugin for APM defaults..."
        & $pipExe install llm-github-models
        Write-Success "GitHub Models plugin installed"
    } else {
        Write-Info "Vanilla mode: Skipping GitHub Models plugin installation"
    }

    # Create .cmd wrapper
    Write-Info "Creating LLM wrapper script..."
    @"
@echo off
"%USERPROFILE%\.apm\runtimes\llm-venv\Scripts\llm.exe" %*
"@ | Set-Content -Path $llmWrapper -Encoding ASCII

    Test-Binary $llmWrapper "LLM"

    Update-UserPath

    # Test installation
    Write-Info "Testing LLM installation..."
    try {
        $ver = & $llmExe --version 2>&1
        Write-Success "LLM runtime installed successfully! Version: $ver"
    } catch {
        Write-WarningText "LLM installed but version check failed. It may still work."
    }

    Write-Host ""
    Write-Info "Next steps:"
    if (-not $Vanilla) {
        Write-Host "1. Set your GitHub token: `$env:GITHUB_TOKEN = 'your_token_here'"
        Write-Host "2. Run with APM: apm run start --runtime=llm"
        Write-Info "GitHub Models provides free access to OpenAI models with your GitHub token"
    } else {
        Write-Host "1. Configure LLM providers: llm keys set <provider>"
        Write-Host "2. Run with APM: apm run start --runtime=llm"
    }
}

Install-Llm
