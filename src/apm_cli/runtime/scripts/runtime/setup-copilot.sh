#!/bin/bash
# Setup script for GitHub Copilot CLI runtime
# Installs @github/copilot with MCP configuration support

set -euo pipefail

# Get the directory of this script for sourcing common utilities
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/setup-common.sh"

# Configuration
COPILOT_PACKAGE="@github/copilot"
VANILLA_MODE=false
NODE_MIN_VERSION="22"
NPM_MIN_VERSION="10"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --vanilla)
            VANILLA_MODE=true
            shift
            ;;
        *)
            # Version specification not supported for Copilot CLI (uses latest from npm)
            shift
            ;;
    esac
done

# Check Node.js version
check_node_version() {
    log_info "Checking Node.js version..."
    
    if ! command -v node >/dev/null 2>&1; then
        log_error "Node.js is not installed"
        log_info "Please install Node.js version $NODE_MIN_VERSION or higher from https://nodejs.org/"
        exit 1
    fi
    
    local node_version=$(node --version | sed 's/v//')
    local node_major=$(echo "$node_version" | cut -d. -f1)
    
    if [[ "$node_major" -lt "$NODE_MIN_VERSION" ]]; then
        log_error "Node.js version $node_version is too old. Required: v$NODE_MIN_VERSION or higher"
        log_info "Please update Node.js from https://nodejs.org/"
        exit 1
    fi
    
    log_success "Node.js version $node_version ✓"
}

# Check npm version
check_npm_version() {
    log_info "Checking npm version..."
    
    if ! command -v npm >/dev/null 2>&1; then
        log_error "npm is not installed"
        log_info "Please install npm version $NPM_MIN_VERSION or higher"
        exit 1
    fi
    
    local npm_version=$(npm --version)
    local npm_major=$(echo "$npm_version" | cut -d. -f1)
    
    if [[ "$npm_major" -lt "$NPM_MIN_VERSION" ]]; then
        log_error "npm version $npm_version is too old. Required: v$NPM_MIN_VERSION or higher"
        log_info "Please update npm with: npm install -g npm@latest"
        exit 1
    fi
    
    log_success "npm version $npm_version ✓"
}



# Install Copilot CLI via npm
install_copilot_cli() {
    log_info "Installing GitHub Copilot CLI..."
    
    # Install globally from public npm registry
    if npm install -g "$COPILOT_PACKAGE"; then
        log_success "Successfully installed $COPILOT_PACKAGE"
    else
        log_error "Failed to install $COPILOT_PACKAGE"
        log_info "This might be due to:"
        log_info "  - Insufficient permissions for global npm install (try with sudo)"
        log_info "  - Network connectivity issues"
        log_info "  - Node.js/npm version compatibility"
        exit 1
    fi
}

# Setup GitHub MCP Server environment for Copilot CLI
setup_github_mcp_environment() {
    log_info "Setting up GitHub MCP Server environment for Copilot CLI..."
    
    # Check for available GitHub tokens for MCP server setup
    local copilot_token=""
    
    # Check token precedence: GITHUB_COPILOT_PAT -> GITHUB_TOKEN -> GITHUB_APM_PAT
    if [[ -n "${GITHUB_COPILOT_PAT:-}" ]]; then
        copilot_token="$GITHUB_COPILOT_PAT"
    elif [[ -n "${GITHUB_TOKEN:-}" ]]; then
        copilot_token="$GITHUB_TOKEN"
    elif [[ -n "${GITHUB_APM_PAT:-}" ]]; then
        copilot_token="$GITHUB_APM_PAT"
    fi
    
    if [[ -n "$copilot_token" ]]; then
        # Set GITHUB_PERSONAL_ACCESS_TOKEN for Copilot CLI's automatic GitHub MCP Server setup
        export GITHUB_PERSONAL_ACCESS_TOKEN="$copilot_token"
        log_success "GitHub MCP Server environment configured"
        log_info "Copilot CLI will automatically set up GitHub MCP Server on first run"
    else
        log_warning "No GitHub token found for automatic MCP server setup"
        log_info "Set GITHUB_COPILOT_PAT, GITHUB_APM_PAT, or GITHUB_TOKEN to enable automatic GitHub MCP Server"
        log_info "You can still configure MCP servers manually using 'apm install'"
    fi
}

# Create basic Copilot CLI directory structure
setup_copilot_directory() {
    log_info "Setting up Copilot CLI directory structure..."
    
    local copilot_config_dir="$HOME/.copilot"
    local mcp_config_file="$copilot_config_dir/mcp-config.json"
    
    # Create config directory if it doesn't exist
    if [[ ! -d "$copilot_config_dir" ]]; then
        log_info "Creating Copilot config directory: $copilot_config_dir"
        mkdir -p "$copilot_config_dir"
    fi
    
    # Create empty MCP configuration template only if file doesn't exist
    if [[ ! -f "$mcp_config_file" ]]; then
        log_info "Creating empty MCP configuration template..."
        cat > "$mcp_config_file" << 'EOF'
{
  "mcpServers": {}
}
EOF
        log_info "Empty MCP configuration created at $mcp_config_file"
        log_info "Use 'apm install' to configure MCP servers"
    else
        log_info "MCP configuration already exists at $mcp_config_file"
    fi
}

# Test Copilot CLI installation
test_copilot_installation() {
    log_info "Testing Copilot CLI installation..."
    
    if command -v copilot >/dev/null 2>&1; then
        if copilot --version >/dev/null 2>&1; then
            local version=$(copilot --version)
            log_success "Copilot CLI installed successfully! Version: $version"
        else
            log_warning "Copilot CLI binary found but version check failed"
            log_info "It may still work, but there might be authentication issues"
        fi
    else
        log_error "Copilot CLI not found in PATH after installation"
        log_info "You may need to restart your terminal or check your npm global installation path"
        exit 1
    fi
}

# Main setup function
setup_copilot() {
    log_info "Setting up GitHub Copilot CLI runtime..."
    
    # Check prerequisites
    check_node_version
    check_npm_version
    
    # Install Copilot CLI (now available on public npm registry)
    install_copilot_cli
    
    # Setup directory structure (unless vanilla mode)
    if [[ "$VANILLA_MODE" == "false" ]]; then
        setup_copilot_directory
        # Setup GitHub MCP Server environment for automatic configuration
        setup_github_mcp_environment
    else
        log_info "Vanilla mode: Skipping APM directory setup"
        log_info "You can configure MCP servers manually in ~/.copilot/mcp-config.json"
    fi
    
    # Test installation
    test_copilot_installation
    
    # Show next steps
    echo ""
    log_info "Next steps:"
    
    if [[ "$VANILLA_MODE" == "false" ]]; then
        echo "1. Set up your APM project with MCP dependencies:"
        echo "   - Initialize project: apm init my-project"
        echo "   - Install MCP servers: apm install"
        echo "2. Then run: apm run start --param name=YourGitHubHandle"
        echo ""
        log_success "✨ GitHub Copilot CLI installed and configured!"
        echo "   - Use 'apm install' to configure MCP servers for your projects"
        echo "   - Copilot CLI provides advanced AI coding assistance with GitHub integration"
        echo "   - Interactive mode available: just run 'copilot'"
    else
        echo "1. Configure Copilot CLI as needed (run 'copilot' for interactive setup)"
        echo "2. Then run with APM: apm run start"
    fi
    
    echo ""
    log_info "GitHub Copilot CLI Features:"
    echo "   - Interactive mode: copilot"
    echo "   - Direct prompts: copilot -p \"your prompt\""
    echo "   - Auto-approval: copilot --allow-all-tools"
    echo "   - Directory access: copilot --add-dir /path/to/directory"
    echo "   - Logging: copilot --log-dir --log-level debug"
}

# Run setup if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    setup_copilot "$@"
fi