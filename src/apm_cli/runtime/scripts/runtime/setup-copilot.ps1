# Setup script for GitHub Copilot CLI runtime (Windows)
# Installs @github/copilot with MCP configuration support

param(
    [switch]$Vanilla
)

$ErrorActionPreference = "Stop"

# Source common utilities
. "$PSScriptRoot\setup-common.ps1"

# Configuration
$CopilotPackage = "@github/copilot"
$NodeMinVersion = 22
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

function Install-CopilotCli {
    Write-Info "Installing GitHub Copilot CLI..."

    try {
        npm install -g $CopilotPackage
        Write-Success "Successfully installed $CopilotPackage"
    } catch {
        Write-ErrorText "Failed to install $CopilotPackage"
        Write-Info "This might be due to:"
        Write-Info "  - Insufficient permissions (try running as Administrator)"
        Write-Info "  - Network connectivity issues"
        Write-Info "  - Node.js/npm version compatibility"
        exit 1
    }
}

function Initialize-CopilotDirectory {
    Write-Info "Setting up Copilot CLI directory structure..."

    $copilotConfigDir = Join-Path $env:USERPROFILE ".copilot"
    $mcpConfigFile = Join-Path $copilotConfigDir "mcp-config.json"

    if (-not (Test-Path $copilotConfigDir)) {
        Write-Info "Creating Copilot config directory: $copilotConfigDir"
        New-Item -ItemType Directory -Force -Path $copilotConfigDir | Out-Null
    }

    if (-not (Test-Path $mcpConfigFile)) {
        Write-Info "Creating empty MCP configuration template..."
        @'
{
  "mcpServers": {}
}
'@ | Set-Content -Path $mcpConfigFile -Encoding UTF8
        Write-Info "Empty MCP configuration created at $mcpConfigFile"
        Write-Info "Use 'apm install' to configure MCP servers"
    } else {
        Write-Info "MCP configuration already exists at $mcpConfigFile"
    }
}

function Initialize-GithubMcpEnvironment {
    Write-Info "Setting up GitHub MCP Server environment for Copilot CLI..."

    $copilotToken = ""
    if ($env:GITHUB_COPILOT_PAT) {
        $copilotToken = $env:GITHUB_COPILOT_PAT
    } elseif ($env:GITHUB_TOKEN) {
        $copilotToken = $env:GITHUB_TOKEN
    } elseif ($env:GITHUB_APM_PAT) {
        $copilotToken = $env:GITHUB_APM_PAT
    }

    if ($copilotToken) {
        $env:GITHUB_PERSONAL_ACCESS_TOKEN = $copilotToken
        Write-Success "GitHub MCP Server environment configured"
        Write-Info "Copilot CLI will automatically set up GitHub MCP Server on first run"
    } else {
        Write-WarningText "No GitHub token found for automatic MCP server setup"
        Write-Info "Set GITHUB_COPILOT_PAT, GITHUB_APM_PAT, or GITHUB_TOKEN to enable automatic GitHub MCP Server"
    }
}

function Test-CopilotInstallation {
    Write-Info "Testing Copilot CLI installation..."

    $copilot = Get-Command copilot -ErrorAction SilentlyContinue
    if ($copilot) {
        try {
            $version = copilot --version
            Write-Success "Copilot CLI installed successfully! Version: $version"
        } catch {
            Write-WarningText "Copilot CLI binary found but version check failed"
        }
    } else {
        Write-ErrorText "Copilot CLI not found in PATH after installation"
        Write-Info "You may need to restart your terminal or check your npm global installation path"
        exit 1
    }
}

# Main setup
Write-Info "Setting up GitHub Copilot CLI runtime..."

Test-NodeVersion
Test-NpmVersion
Install-CopilotCli

if (-not $Vanilla) {
    Initialize-CopilotDirectory
    Initialize-GithubMcpEnvironment
} else {
    Write-Info "Vanilla mode: Skipping APM directory setup"
    Write-Info "You can configure MCP servers manually in ~/.copilot/mcp-config.json"
}

Test-CopilotInstallation

Write-Host ""
Write-Info "Next steps:"
if (-not $Vanilla) {
    Write-Host "1. Set up your APM project with MCP dependencies:"
    Write-Host "   - Initialize project: apm init my-project"
    Write-Host "   - Install MCP servers: apm install"
    Write-Host "2. Run: apm run start --param name=YourName"
} else {
    Write-Host "1. Configure Copilot CLI manually"
    Write-Host "2. Then run with APM: apm run start"
}
