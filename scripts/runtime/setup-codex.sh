#!/bin/bash
# Setup script for Codex runtime
# Downloads Codex binary from GitHub releases and configures with GitHub Models
# Automatically sets up GitHub MCP Server integration when GITHUB_TOKEN or GITHUB_APM_PAT is available

set -euo pipefail

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source the centralized GitHub token helper  
# Handle both embedded execution (RuntimeManager) and direct execution (tests)
if [[ -f "$SCRIPT_DIR/github-token-helper.sh" ]]; then
    # Embedded execution - token helper is in same directory
    source "$SCRIPT_DIR/github-token-helper.sh"
elif [[ -f "$SCRIPT_DIR/../github-token-helper.sh" ]]; then
    # Direct execution - token helper is in parent directory
    source "$SCRIPT_DIR/../github-token-helper.sh"
else
    echo "Warning: GitHub token helper not found, using fallback authentication"
fi
source "$SCRIPT_DIR/setup-common.sh"

# Configuration
CODEX_REPO="openai/codex"
# Pin to a known stable release for security and reproducibility (#662).
# Users can override with: apm runtime setup codex --version <version> (e.g. 'latest')
CODEX_VERSION="rust-v0.118.0"
VANILLA_MODE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --vanilla)
            VANILLA_MODE=true
            shift
            ;;
        *)
            # If it's not --vanilla and not empty, treat it as version
            if [[ -n "$1" && "$1" != "--vanilla" ]]; then
                CODEX_VERSION="$1"
            fi
            shift
            ;;
    esac
done

setup_codex() {
    log_info "Setting up Codex runtime..."
    
    # Detect platform using detect_platform from common utilities
    detect_platform
    
    # Map APM platform format to Codex binary format
    local codex_platform
    case "$DETECTED_PLATFORM" in
        darwin-arm64)
            codex_platform="aarch64-apple-darwin"
            ;;
        darwin-x86_64)
            codex_platform="x86_64-apple-darwin"
            ;;
        linux-x86_64)
            codex_platform="x86_64-unknown-linux-gnu"
            ;;
        linux-aarch64)
            codex_platform="aarch64-unknown-linux-gnu"
            ;;
        *)
            log_error "Unsupported platform: $DETECTED_PLATFORM"
            exit 1
            ;;
    esac
    
    # Ensure APM runtime directory exists
    ensure_apm_runtime_dir
    
    # Set up paths
    local runtime_dir="$HOME/.apm/runtimes"
    local codex_binary="$runtime_dir/codex"
    local codex_config_dir="$HOME/.codex"
    local codex_config="$codex_config_dir/config.toml"
    local temp_dir="/tmp/apm-codex-install"
    
    # Create temp directory
    mkdir -p "$temp_dir"
    
    # Determine download URL for the tar.gz file
    local download_url
    if [[ "$CODEX_VERSION" == "latest" ]]; then
        # Fetch the latest release tag from GitHub API
        log_info "Fetching latest Codex release information..."
        local latest_release_url="https://api.github.com/repos/$CODEX_REPO/releases/latest"
        local latest_tag
        
        # Try to get the latest release tag using curl
        if command -v curl >/dev/null 2>&1; then
            # Use authenticated request if GITHUB_TOKEN or GITHUB_APM_PAT is available
            if [[ -n "${GITHUB_TOKEN:-}" ]]; then
                log_info "Using authenticated GitHub API request (GITHUB_TOKEN - ${#GITHUB_TOKEN} chars)"
                local auth_header="Authorization: Bearer ${GITHUB_TOKEN}"
                latest_tag=$(curl -s -H "$auth_header" "$latest_release_url" | grep '"tag_name":' | sed -E 's/.*"tag_name":[[:space:]]*"([^"]+)".*/\1/')
            elif [[ -n "${GITHUB_APM_PAT:-}" ]]; then
                log_info "Using authenticated GitHub API request (GITHUB_APM_PAT - ${#GITHUB_APM_PAT} chars)"
                local auth_header="Authorization: Bearer ${GITHUB_APM_PAT}"
                latest_tag=$(curl -s -H "$auth_header" "$latest_release_url" | grep '"tag_name":' | sed -E 's/.*"tag_name":[[:space:]]*"([^"]+)".*/\1/')
            else
                log_info "🚨 DEBUG: No tokens available - GITHUB_TOKEN and GITHUB_APM_PAT both unset!"
                log_info "Using unauthenticated GitHub API request (60 requests/hour limit)"
                latest_tag=$(curl -s "$latest_release_url" | grep '"tag_name":' | sed -E 's/.*"tag_name":[[:space:]]*"([^"]+)".*/\1/')
            fi
        else
            log_error "curl is required to fetch latest release information"
            exit 1
        fi
        
        # Verify we got a valid tag, fallback to unauthenticated if auth failed
        if [[ -z "$latest_tag" || "$latest_tag" == "null" ]]; then
            log_info "Authenticated request did not return a valid tag, retrying without authentication..."
            latest_tag=$(curl -s "$latest_release_url" | grep '"tag_name":' | sed -E 's/.*"tag_name":[[:space:]]*"([^"]+)".*/\1/')
        fi
        
        if [[ -z "$latest_tag" || "$latest_tag" == "null" ]]; then
            log_error "Failed to fetch latest release tag from GitHub API"
            log_error "No fallback available. Please check your internet connection or specify a specific version."
            exit 1
        fi
        
        log_info "Using Codex release: $latest_tag"
        download_url="https://github.com/$CODEX_REPO/releases/download/$latest_tag/codex-$codex_platform.tar.gz"
    else
        download_url="https://github.com/$CODEX_REPO/releases/download/$CODEX_VERSION/codex-$codex_platform.tar.gz"
    fi
    
    # Download and extract Codex binary
    log_info "Downloading Codex binary for $codex_platform..."
    local tar_file="$temp_dir/codex-$codex_platform.tar.gz"
    download_file "$download_url" "$tar_file" "Codex binary archive"
    
    # Extract the binary
    log_info "Extracting Codex binary..."
    cd "$temp_dir"
    tar -xzf "$tar_file"
    
    # Find the extracted binary (should be named 'codex-{platform}' or just 'codex')
    local extracted_binary=""
    if [[ -f "$temp_dir/codex" ]]; then
        extracted_binary="$temp_dir/codex"
    elif [[ -f "$temp_dir/codex-$codex_platform" ]]; then
        extracted_binary="$temp_dir/codex-$codex_platform"
    else
        log_error "Codex binary not found in extracted archive. Contents:"
        ls -la "$temp_dir"
        exit 1
    fi
    
    # Move to final location
    mv "$extracted_binary" "$codex_binary"
    
    # Clean up temp directory
    rm -rf "$temp_dir"
    
    # Verify binary
    verify_binary "$codex_binary" "Codex"
    
    # Create configuration if not in vanilla mode
    if [[ "$VANILLA_MODE" == "false" ]]; then
        # Create Codex config directory
        if [[ ! -d "$codex_config_dir" ]]; then
            log_info "Creating Codex config directory: $codex_config_dir"
            mkdir -p "$codex_config_dir"
        fi
        
        # Create Codex configuration for GitHub Models only
        log_info "Creating Codex configuration for GitHub Models (APM default)..."
        
        # Use centralized token management for GitHub Models
        # CRITICAL: GitHub Models API requires USER-SCOPED tokens, not org-scoped fine-grained PATs
        setup_github_tokens
        
        local models_token
        models_token=$(get_token_for_runtime "models")
        
        local github_token_var="GITHUB_TOKEN"
        if [[ -n "$models_token" ]]; then
            if [[ -n "${GITHUB_TOKEN:-}" ]]; then
                github_token_var="GITHUB_TOKEN"
                log_info "Using GITHUB_TOKEN for GitHub Models authentication (user-scoped PAT)"
            elif [[ -n "${GITHUB_APM_PAT:-}" ]]; then
                github_token_var="GITHUB_APM_PAT"
                log_warning "Using GITHUB_APM_PAT for GitHub Models (may not work if org-scoped)"
                log_info "Note: GitHub Models requires user-scoped PATs, not org fine-grained PATs"
            else
                github_token_var="GITHUB_TOKEN"
                log_info "No GitHub token found - you'll need to set GITHUB_TOKEN (user-scoped PAT)"
            fi
        else
            log_info "No GitHub token found - you'll need to set GITHUB_TOKEN (user-scoped PAT)"
        fi
        
        cat > "$codex_config" << EOF
model_provider = "github-models"
model = "openai/gpt-4o"

[model_providers.github-models]
name = "GitHub Models"
base_url = "https://models.github.ai/inference/"
env_key = "$github_token_var"
wire_api = "responses"
EOF
        
        log_success "Codex configuration created at $codex_config"
        log_info "Using Codex $CODEX_VERSION."
        log_info "Override with: apm runtime setup codex --version <version> (e.g. 'latest')"
        log_info "APM configured Codex with GitHub Models as default provider"
        log_info "Use 'apm install' to configure MCP servers for your projects"
    else
        log_info "Vanilla mode: Skipping APM configuration - Codex will use its native defaults"
    fi
    
    # Update PATH
    ensure_path_updated
    
    # Test installation
    log_info "Testing Codex installation..."
    if "$codex_binary" --version >/dev/null 2>&1; then
        local version=$("$codex_binary" --version)
        log_success "Codex runtime installed successfully! Version: $version"
    else
        log_warning "Codex binary installed but version check failed. It may still work."
    fi
    
    # Show next steps
    echo ""
    log_info "Next steps:"
    if [[ "$VANILLA_MODE" == "false" ]]; then
        echo "1. Set up your APM project with MCP dependencies:"
        echo "   - Initialize project: apm init my-project"
        echo "   - Install MCP servers: apm install"
        echo "2. Set your GitHub token: export GITHUB_TOKEN=your_token_here (or GITHUB_APM_PAT=your_token_here)"
        echo "3. Then run: apm run start --param name=YourName"
        echo ""
        log_success "✨ Codex installed and configured with GitHub Models!"
        echo "   - Use 'apm install' to configure MCP servers for your projects"
        echo "   - GitHub Models provides free access to OpenAI models with your GitHub token"
    else
        echo "1. Configure Codex with your preferred provider (see: codex --help)"
        echo "2. Then run with APM: apm run start"
    fi
}

# Run setup if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    setup_codex "$@"
fi
