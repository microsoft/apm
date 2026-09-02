# Setup script for Google Gemini CLI runtime (Windows)
# Installs @google/gemini-cli with MCP configuration support

param(
    [switch]$Vanilla
)

$ErrorActionPreference = "Stop"

# Source common utilities
. "$PSScriptRoot\setup-common.ps1"

# Configuration
$GeminiPackage = "@google/gemini-cli"
$NodeMinVersion = 20
$NpmMinVersion = 10

function Test-NodeVersion {
    Write-Info "Checking Node.js version..."

    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        Write-ErrorText "Node.js is not installed"
        Write-Info "Please install Node.js version $NodeMinVersion or higher from https://nodejs.org/"
        exit 1
    }

    $nodeVersion = (node --version) -replace '^v', ''
    $nodeMajor = [int]($nodeVersion.Split('.')[0])

    if ($nodeMajor -lt $NodeMinVersion) {
        Write-ErrorText "Node.js version $nodeVersion is too old. Required: v$NodeMinVersion or higher"
        Write-Info "Please update Node.js from https://nodejs.org/"
        exit 1
    }

    Write-Success "Node.js version $nodeVersion"
}

function Test-NpmVersion {
    Write-Info "Checking npm version..."

    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        Write-ErrorText "npm is not installed"
        Write-Info "Please install npm version $NpmMinVersion or higher"
        exit 1
    }

    $npmVersion = npm --version
    $npmMajor = [int]($npmVersion.Split('.')[0])

    if ($npmMajor -lt $NpmMinVersion) {
        Write-ErrorText "npm version $npmVersion is too old. Required: v$NpmMinVersion or higher"
        Write-Info "Please update npm with: npm install -g npm@latest"
        exit 1
    }

    Write-Success "npm version $npmVersion"
}

function Install-GeminiCli {
    Write-Info "Installing Google Gemini CLI..."

    try {
        npm install -g $GeminiPackage
        Write-Success "Successfully installed $GeminiPackage"
    } catch {
        Write-ErrorText "Failed to install $GeminiPackage"
        Write-Info "This might be due to:"
        Write-Info "  - Insufficient permissions (try running as Administrator)"
        Write-Info "  - Network connectivity issues"
        Write-Info "  - Node.js/npm version compatibility"
        exit 1
    }
}

function Initialize-GeminiDirectory {
    Write-Info "Setting up Gemini CLI directory structure..."

    $geminiConfigDir = Join-Path $env:USERPROFILE ".gemini"
    $settingsFile = Join-Path $geminiConfigDir "settings.json"

    if (-not (Test-Path $geminiConfigDir)) {
        Write-Info "Creating Gemini config directory: $geminiConfigDir"
        New-Item -ItemType Directory -Force -Path $geminiConfigDir | Out-Null
    }

    if (-not (Test-Path $settingsFile)) {
        Write-Info "Creating settings.json template..."
        @'
{
  "mcpServers": {}
}
'@ | Set-Content -Path $settingsFile -Encoding UTF8
        Write-Info "Settings created at $settingsFile"
        Write-Info "Use 'apm install' to configure MCP servers"
    } else {
        Write-Info "Settings already exist at $settingsFile"
    }
}

function Test-GeminiInstallation {
    Write-Info "Testing Gemini CLI installation..."

    $gemini = Get-Command gemini -ErrorAction SilentlyContinue
    if ($gemini) {
        try {
            $version = gemini --version
            Write-Success "Gemini CLI installed successfully! Version: $version"
        } catch {
            Write-WarningText "Gemini CLI binary found but version check failed"
        }
    } else {
        Write-ErrorText "Gemini CLI not found in PATH after installation"
        Write-Info "You may need to restart your terminal or check your npm global installation path"
        exit 1
    }
}

# Main setup
Write-Info "Setting up Google Gemini CLI runtime..."

Test-NodeVersion
Test-NpmVersion
Install-GeminiCli

if (-not $Vanilla) {
    Initialize-GeminiDirectory
} else {
    Write-Info "Vanilla mode: Skipping APM directory setup"
    Write-Info "You can configure settings manually in ~/.gemini/settings.json"
}

Test-GeminiInstallation

Write-Host ""
Write-Info "Next steps:"
if (-not $Vanilla) {
    Write-Host "1. Authenticate with Google:"
    Write-Host "   - Run 'gemini' and follow the browser login flow (free tier), or"
    Write-Host "   - Set GOOGLE_API_KEY for Gemini API key authentication, or"
    Write-Host "   - Set GOOGLE_GENAI_USE_VERTEXAI=true + GOOGLE_CLOUD_PROJECT for Vertex AI"
    Write-Host "2. Set up your APM project with MCP dependencies:"
    Write-Host "   - Initialize project: apm init my-project"
    Write-Host "   - Install MCP servers: apm install"
    Write-Host "3. Run: apm run start --param name=YourName"
} else {
    Write-Host "1. Configure Gemini CLI manually"
    Write-Host "2. Then run with APM: apm run start"
}
