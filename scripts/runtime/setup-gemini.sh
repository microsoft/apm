#!/bin/bash
# Setup script for Google Gemini CLI runtime
# Installs @google/gemini-cli with MCP configuration support

set -euo pipefail

# Get the directory of this script for sourcing common utilities
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/setup-common.sh"

# Configuration
GEMINI_PACKAGE="@google/gemini-cli"
VANILLA_MODE=false
NODE_MIN_VERSION="20"
NPM_MIN_VERSION="10"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --vanilla)
            VANILLA_MODE=true
            shift
            ;;
        *)
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

    log_success "Node.js version $node_version"
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

    log_success "npm version $npm_version"
}

# Install Gemini CLI via npm
install_gemini_cli() {
    log_info "Installing Google Gemini CLI..."

    if npm install -g "$GEMINI_PACKAGE"; then
        log_success "Successfully installed $GEMINI_PACKAGE"
    else
        log_error "Failed to install $GEMINI_PACKAGE"
        log_info "This might be due to:"
        log_info "  - Insufficient permissions for global npm install (try with sudo)"
        log_info "  - Network connectivity issues"
        log_info "  - Node.js/npm version compatibility"
        exit 1
    fi
}

# Create Gemini CLI directory structure and config
setup_gemini_directory() {
    log_info "Setting up Gemini CLI directory structure..."

    local gemini_config_dir="$HOME/.gemini"
    local settings_file="$gemini_config_dir/settings.json"

    # Create config directory if it doesn't exist
    if [[ ! -d "$gemini_config_dir" ]]; then
        log_info "Creating Gemini config directory: $gemini_config_dir"
        mkdir -p "$gemini_config_dir"
    fi

    # Create empty settings.json with MCP section only if file doesn't exist
    if [[ ! -f "$settings_file" ]]; then
        log_info "Creating settings.json template..."
        cat > "$settings_file" << 'EOF'
{
  "mcpServers": {}
}
EOF
        log_info "Settings created at $settings_file"
        log_info "Use 'apm install' to configure MCP servers"
    else
        log_info "Settings already exist at $settings_file"
    fi
}

# Test Gemini CLI installation
test_gemini_installation() {
    log_info "Testing Gemini CLI installation..."

    if command -v gemini >/dev/null 2>&1; then
        if gemini --version >/dev/null 2>&1; then
            local version=$(gemini --version)
            log_success "Gemini CLI installed successfully! Version: $version"
        else
            log_warning "Gemini CLI binary found but version check failed"
            log_info "It may still work, but there might be configuration issues"
        fi
    else
        log_error "Gemini CLI not found in PATH after installation"
        log_info "You may need to restart your terminal or check your npm global installation path"
        exit 1
    fi
}

# Main setup function
setup_gemini() {
    log_info "Setting up Google Gemini CLI runtime..."

    # Check prerequisites
    check_node_version
    check_npm_version

    # Install Gemini CLI
    install_gemini_cli

    # Setup directory structure (unless vanilla mode)
    if [[ "$VANILLA_MODE" == "false" ]]; then
        setup_gemini_directory
    else
        log_info "Vanilla mode: Skipping APM directory setup"
        log_info "You can configure settings manually in ~/.gemini/settings.json"
    fi

    # Test installation
    test_gemini_installation

    # Show next steps
    echo ""
    log_info "Next steps:"

    if [[ "$VANILLA_MODE" == "false" ]]; then
        echo "1. Authenticate with Google:"
        echo "   - Run 'gemini' and follow the browser login flow (free tier), or"
        echo "   - Set GOOGLE_API_KEY for Gemini API key authentication, or"
        echo "   - Set GOOGLE_GENAI_USE_VERTEXAI=true + GOOGLE_CLOUD_PROJECT for Vertex AI"
        echo "2. Set up your APM project with MCP dependencies:"
        echo "   - Initialize project: apm init my-project"
        echo "   - Install MCP servers: apm install"
        echo "3. Then run: apm run start --param name=YourName"
        echo ""
        log_success "Gemini CLI installed and configured!"
        echo "   - Use 'apm install' to configure MCP servers for your projects"
        echo "   - Free tier: 60 requests/min with a personal Google account"
        echo "   - Interactive mode available: just run 'gemini'"
    else
        echo "1. Configure Gemini CLI as needed (run 'gemini' for interactive setup)"
        echo "2. Then run with APM: apm run start"
    fi

    echo ""
    log_info "Gemini CLI Features:"
    echo "   - Interactive mode: gemini"
    echo "   - Sandboxed mode: gemini -s"
    echo "   - Custom model: gemini -m gemini-3.1-pro-preview"
    echo "   - Yolo mode (auto-approve): gemini -y"
}

# Run setup if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    setup_gemini "$@"
fi
