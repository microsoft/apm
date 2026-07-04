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
# Last Codex minor version that works with GitHub Models without wire_api=chat (#605)
LAST_COMPAT_VERSION_MINOR=115

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

extract_release_tag() {
    grep '"tag_name":' | head -n 1 | sed -E 's/.*"tag_name":[[:space:]]*"([^"]+)".*/\1/'
}

extract_release_asset_digest() {
    local asset_name="$1"

    if command -v jq >/dev/null 2>&1; then
        jq -r --arg asset_name "$asset_name" \
            '.assets[]? | select(.name == $asset_name) | .digest // empty' \
            | head -n 1
        return 0
    fi

    awk -v asset_name="$asset_name" '
        function json_string_value(record, key, value) {
            value = record
            sub(".*\"" key "\"[[:space:]]*:[[:space:]]*\"", "", value)
            if (value == record) {
                return ""
            }
            sub("\".*", "", value)
            return value
        }

        /"name"[[:space:]]*:/ {
            in_asset = (json_string_value($0, "name") == asset_name)
        }
        in_asset && /"digest"[[:space:]]*:/ {
            print json_string_value($0, "digest")
            exit
        }
        in_asset && /"browser_download_url"[[:space:]]*:/ {
            exit
        }
    ' RS=,
}

fetch_github_api() {
    local url="$1"
    local use_auth="$2"
    local token=""

    if [[ "$use_auth" == "true" ]]; then
        if [[ -n "${GITHUB_TOKEN:-}" ]]; then
            token="$GITHUB_TOKEN"
        elif [[ -n "${GITHUB_APM_PAT:-}" ]]; then
            token="$GITHUB_APM_PAT"
        elif [[ -n "${GH_TOKEN:-}" ]]; then
            token="$GH_TOKEN"
        fi
    fi

    if command -v curl >/dev/null 2>&1; then
        if [[ -n "$token" ]]; then
            curl -fsSL -H "Authorization: Bearer $token" "$url"
        else
            curl -fsSL "$url"
        fi
    elif command -v wget >/dev/null 2>&1; then
        if [[ -n "$token" ]]; then
            wget -qO- --header="Authorization: Bearer $token" "$url"
        else
            wget -qO- "$url"
        fi
    else
        log_error "Neither curl nor wget is available. Please install one of them."
        exit 1
    fi
}

fetch_release_metadata() {
    local url="$1"
    local response=""

    if [[ -n "${GITHUB_TOKEN:-}" || -n "${GITHUB_APM_PAT:-}" || -n "${GH_TOKEN:-}" ]]; then
        if response="$(fetch_github_api "$url" true 2>/dev/null)" \
            && [[ -n "$(printf '%s\n' "$response" | extract_release_tag)" ]]; then
            printf '%s' "$response"
            return 0
        fi
    fi

    if response="$(fetch_github_api "$url" false 2>/dev/null)" \
        && [[ -n "$(printf '%s\n' "$response" | extract_release_tag)" ]]; then
        printf '%s' "$response"
        return 0
    fi

    return 1
}

sha256_file() {
    local file_path="$1"

    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$file_path" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$file_path" | awk '{print $1}'
    else
        log_error "Neither sha256sum nor shasum is available. Please install one of them."
        exit 1
    fi
}

verify_archive_checksum() {
    local archive_path="$1"
    local expected_digest="$2"
    local normalized_expected
    local actual_digest
    local normalized_actual

    normalized_expected="$(printf '%s' "$expected_digest" | sed 's/^sha256://' | tr '[:upper:]' '[:lower:]')"
    if [[ ! "$normalized_expected" =~ ^[0-9a-f]{64}$ ]]; then
        log_error "GitHub release metadata did not include a valid SHA-256 digest."
        log_error "Try a different Codex version or retry after upstream release metadata publishes a digest."
        exit 1
    fi

    actual_digest="$(sha256_file "$archive_path")"
    normalized_actual="$(printf '%s' "$actual_digest" | tr '[:upper:]' '[:lower:]')"

    if [[ "$normalized_actual" != "$normalized_expected" ]]; then
        log_error "Checksum verification failed for downloaded Codex archive."
        log_error "Expected SHA-256: ${normalized_expected:0:16}..."
        log_error "Actual SHA-256: ${normalized_actual:0:16}..."
        log_error "Re-run the command to retry. If this persists, the release may be compromised."
        exit 1
    fi

    echo "[+] Verified Codex archive checksum"
}

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
    local temp_dir
    local release_metadata_url
    local release_metadata
    local release_tag
    local archive_name
    local archive_digest
    
    temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/apm-codex-install.XXXXXX")"
    cleanup_codex_temp_dir() {
        rm -rf "${temp_dir:-}"
    }
    trap cleanup_codex_temp_dir EXIT
    
    # Determine release metadata and download URL for the tar.gz file
    local download_url
    if [[ "$CODEX_VERSION" == "latest" ]]; then
        log_info "Fetching latest Codex release information..."
        release_metadata_url="https://api.github.com/repos/$CODEX_REPO/releases/latest"
    else
        release_metadata_url="https://api.github.com/repos/$CODEX_REPO/releases/tags/$CODEX_VERSION"
    fi

    if ! release_metadata="$(fetch_release_metadata "$release_metadata_url")"; then
        log_error "Failed to fetch Codex release metadata from GitHub API."
        log_error "No fallback available. Please check your internet connection or specify a valid version."
        exit 1
    fi

    release_tag="$(printf '%s\n' "$release_metadata" | extract_release_tag)"
    if [[ -z "$release_tag" || "$release_tag" == "null" ]]; then
        log_error "Failed to determine Codex release tag from GitHub API."
        exit 1
    fi

    if [[ "$CODEX_VERSION" == "latest" ]]; then
        log_info "Using Codex release: $release_tag"
        CODEX_VERSION="$release_tag"
    fi

    archive_name="codex-$codex_platform.tar.gz"
    # Depends on GitHub Releases API asset.digest metadata; fail closed if it disappears.
    archive_digest="$(printf '%s\n' "$release_metadata" | extract_release_asset_digest "$archive_name")"
    if [[ -z "$archive_digest" ]]; then
        log_error "Failed to find checksum metadata for $archive_name."
        log_error "Try a different Codex version or retry after upstream release metadata publishes a digest."
        exit 1
    fi

    download_url="https://github.com/$CODEX_REPO/releases/download/$release_tag/$archive_name"
    
    # Download and extract Codex binary
    log_info "Downloading Codex binary for $codex_platform..."
    local tar_file="$temp_dir/$archive_name"
    download_file "$download_url" "$tar_file" "Codex binary archive"
    verify_archive_checksum "$tar_file" "$archive_digest"
    
    # Extract the binary
    log_info "Extracting Codex binary..."
    tar -xzf "$tar_file" -C "$temp_dir"
    
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
    trap - EXIT
    
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

        # Version compatibility check
        codex_minor=$(echo "$CODEX_VERSION" | sed -n 's/^rust-v0\.\([0-9]*\).*/\1/p')
        if [ -n "$codex_minor" ] && [ "$codex_minor" -gt "$LAST_COMPAT_VERSION_MINOR" ] 2>/dev/null; then
            echo ""
            log_warning "codex >= v0.116 requires wire_api=chat configuration for GitHub Models compatibility."
            log_warning "The generated config uses wire_api=responses, which returns 404 with GitHub Models."
            log_warning "To fix, update wire_api in $codex_config:"
            log_warning "  wire_api = \"chat\""
            log_warning "Or install an older compatible version: apm runtime setup codex --version rust-v0.115.0"
            echo ""
        fi

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
