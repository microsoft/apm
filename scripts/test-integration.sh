#!/bin/bash
# Integration testing orchestrator for both CI and local environments.
#
# This script is intentionally a thin wrapper. Per-test gating
# (tokens, runtimes, binary, network) lives in
# tests/integration/conftest.py via the marker registry shipped in
# PR1 of #1166 (microsoft/apm#1167). PR2 of #1166 retired the
# per-file pytest enumeration that previously lived here in favour of
# a single ``pytest tests/integration/`` invocation. New integration
# test files dropped into tests/integration/ are picked up
# automatically; add the right ``requires_*`` marker (see
# pyproject.toml [tool.pytest.ini_options].markers) and the registry
# will skip the test when its precondition is missing.
#
# This script's responsibilities are now narrow:
#   - resolve GitHub / ADO tokens (via scripts/github-token-helper.sh)
#   - detect platform and execution environment (CI vs local)
#   - locate or build the apm PyInstaller binary
#   - install runtimes the binary needs (codex / copilot / llm)
#   - install python test deps (uv preferred)
#   - invoke pytest tests/integration/ exactly once
#
# To run a focused subset locally, invoke pytest directly:
#   APM_E2E_TESTS=1 pytest tests/integration/test_X.py -v
# (the marker registry will still auto-skip preconditions that the
# local env doesn't satisfy)
#
# - CI mode: Uses pre-built artifacts from build job.
# - Local mode: Builds the binary up-front.

set -euo pipefail

# Global variables
USE_EXISTING_BINARY=false

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Source the GitHub token management helper
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/github-token-helper.sh"

log_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

# Check prerequisites 
check_prerequisites() {
    log_info "Checking prerequisites..."
    
    # Use centralized token management
    if setup_github_tokens; then
        log_success "GitHub tokens configured successfully"
    else
        log_error "GitHub token setup failed"
        return 1
    fi
    
    # Set up GitHub tokens for testing
    # No specific NPM authentication needed for public runtimes
    if [[ -n "${GITHUB_APM_PAT:-}" ]]; then
        log_success "GITHUB_APM_PAT is set (APM module access)"
        export GITHUB_APM_PAT="${GITHUB_APM_PAT}"
    fi
    
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        log_success "GITHUB_TOKEN is set (GitHub Models access)"
        export GITHUB_TOKEN="${GITHUB_TOKEN}"
    fi
}

# Detect platform (like CI matrix does)
detect_platform() {
    local os=$(uname -s | tr '[:upper:]' '[:lower:]')
    local arch=$(uname -m)
    
    case "$os" in
        linux*)
            case "$arch" in
                x86_64|amd64)
                    BINARY_NAME="apm-linux-x86_64"
                    ;;
                aarch64|arm64)
                    BINARY_NAME="apm-linux-arm64"
                    ;;
                *)
                    log_error "Unsupported Linux architecture: $arch"
                    exit 1
                    ;;
            esac
            ;;
        darwin*)
            case "$arch" in
                x86_64)
                    BINARY_NAME="apm-darwin-x86_64"
                    ;;
                arm64)
                    BINARY_NAME="apm-darwin-arm64"
                    ;;
                *)
                    log_error "Unsupported macOS architecture: $arch"
                    exit 1
                    ;;
            esac
            ;;
        *)
            log_error "Unsupported operating system: $os"
            exit 1
            ;;
    esac
    
    log_info "Detected platform: $BINARY_NAME"
}

# Detect environment and check if we should build or use existing binary
detect_environment() {
    log_info "Detecting environment..."
    
    # Check if we're in CI with pre-built artifacts (binary exists in ./dist/)
    # The binary is located at ./dist/$BINARY_NAME/apm (directory structure)
    if [[ -d "./dist/$BINARY_NAME" ]] && [[ -f "./dist/$BINARY_NAME/apm" ]]; then
        USE_EXISTING_BINARY=true
        log_info "Found existing binary: ./dist/$BINARY_NAME/apm (CI mode)"
    else
        USE_EXISTING_BINARY=false
        log_info "No existing binary found at ./dist/$BINARY_NAME/apm, will build locally"
        # Debug: show what's actually in dist/ to diagnose artifact download issues
        if [[ -d "./dist" ]]; then
            log_info "Contents of ./dist/: $(ls -la ./dist/ 2>/dev/null | head -10)"
        else
            log_info "No ./dist/ directory exists"
        fi
    fi
}
# Build binary (like CI build job does) - only if needed
build_binary() {
    if [[ "$USE_EXISTING_BINARY" == "true" ]]; then
        log_info "=== Skipping binary build (using existing CI artifact) ==="
        return 0
    fi
    
    log_info "=== Building APM binary (local mode) ==="
    
    # Install Python dependencies (like CI does)
    log_info "Installing Python dependencies..."
    if command -v uv >/dev/null 2>&1; then
        log_info "Using uv for binary build dependencies..."
        if [[ -d ".venv" ]]; then
            log_info "Virtual environment already exists, reusing it..."
        else
            uv venv
        fi
        source .venv/bin/activate
        uv pip install -e ".[dev]"
        uv pip install pyinstaller
    else
        log_info "Using pip for binary build dependencies..."
        python -m pip install --upgrade pip
        pip install -e .
        pip install pyinstaller
    fi
    
    # Build binary (like CI does)
    log_info "Building binary with build-binary.sh..."
    chmod +x scripts/build-binary.sh
    ./scripts/build-binary.sh
    
    # Verify binary was created
    # The build script creates ./dist/$BINARY_NAME/apm (directory structure)
    if [[ ! -f "./dist/$BINARY_NAME/apm" ]]; then
        log_error "Binary not found: ./dist/$BINARY_NAME/apm"
        exit 1
    fi
    
    log_success "Binary built: ./dist/$BINARY_NAME/apm"
}

# Set up binary for testing (exactly like CI does)
setup_binary_for_testing() {
    log_info "=== Setting up binary for testing (mirroring CI process) ==="
    
    # The binary is located at ./dist/$BINARY_NAME/apm (directory structure)
    BINARY_PATH="./dist/$BINARY_NAME/apm"
    
    # Make binary executable (like CI does)
    chmod +x "$BINARY_PATH"
    
    # Create APM symlink for testing (exactly like CI does)
    ln -sf "$(pwd)/dist/$BINARY_NAME/apm" "$(pwd)/apm"
    
    # Add current directory to PATH (like CI does)
    export PATH="$(pwd):$PATH"
    
    # Verify setup
    if ! command -v apm >/dev/null 2>&1; then
        log_error "APM not found in PATH after setup"
        exit 1
    fi
    
    local version=$(apm --version)
    log_success "APM binary ready for testing: $version"
}

# Set up runtimes (codex/llm/copilot) - Integration Testing Coverage!
setup_runtimes() {
    log_info "=== Setting up runtimes for integration tests ==="
    
    # Set up GitHub Copilot CLI runtime (recommended default)
    log_info "Setting up GitHub Copilot CLI runtime..."
    if ! ./apm runtime setup copilot; then
        log_error "Failed to set up GitHub Copilot CLI runtime"
        exit 1
    fi
    
    # Set up codex runtime
    log_info "Setting up Codex runtime..."
    if ! ./apm runtime setup codex; then
        log_error "Failed to set up Codex runtime"
        exit 1
    fi
    
    # Set up LLM runtime  
    log_info "Setting up LLM runtime..."
    if ! ./apm runtime setup llm; then
        log_error "Failed to set up LLM runtime"
        exit 1
    fi
    
    # Add runtime paths to current session PATH
    log_info "Adding runtime paths to current session..."
    RUNTIME_PATH="$HOME/.apm/runtimes"
    export PATH="$RUNTIME_PATH:$PATH"
    
    # Verify runtimes are available
    log_info "Verifying runtime installations..."
    
    # Check GitHub Copilot CLI
    if command -v copilot >/dev/null 2>&1; then
        local copilot_version=$(copilot --version 2>&1 || echo "unknown")
        log_success "GitHub Copilot CLI ready: $copilot_version"
    else
        log_error "GitHub Copilot CLI not found in PATH after setup"
        exit 1
    fi
    
    # Check codex
    if command -v codex >/dev/null 2>&1; then
        local codex_version=$(codex --version 2>&1 || echo "unknown")
        log_success "Codex runtime ready: $codex_version"
    else
        log_error "Codex not found in PATH after setup"
        echo "PATH: $PATH"
        echo "Looking for codex in: $RUNTIME_PATH"
        ls -la "$RUNTIME_PATH" || echo "Runtime directory not found"
        exit 1
    fi
    
    # Check LLM wrapper
    local llm_path="$HOME/.apm/runtimes/llm"
    if [[ -x "$llm_path" ]]; then
        log_success "LLM runtime ready at: $llm_path"
    else
        log_error "LLM runtime not found at: $llm_path"
        exit 1
    fi
    
    log_success "All runtimes configured successfully (Copilot, Codex, LLM)"
}

# Install test dependencies (like CI does)
install_test_dependencies() {
    log_info "=== Installing test dependencies ==="
    
    # Check if uv is available, otherwise use pip
    if command -v uv >/dev/null 2>&1; then
        log_info "Using uv for dependency installation..."
        
        # Check if .venv already exists (CI mode where workflow already ran uv sync)
        if [[ -d ".venv" ]]; then
            log_info "Virtual environment already exists, activating it..."
            source .venv/bin/activate
        else
            log_info "Creating new virtual environment..."
            uv venv --python 3.12 || uv venv  # Try 3.12 first, fallback to default
            source .venv/bin/activate
            uv pip install -e ".[dev]"
        fi
    else
        log_info "Using pip for dependency installation..."
        pip install -e ".[dev]"
    fi
    
    log_success "Test dependencies installed"
}

# Run integration tests via marker-driven discovery (issue #1166).
#
# All per-test gating (tokens, runtimes, binary, network) lives in
# tests/integration/conftest.py via the _MARKER_CHECKS registry shipped
# in PR1 (#1167). This function is intentionally a thin wrapper: pytest
# discovers test files, the marker registry skips what the env can't
# satisfy, and one exit code reports the result.
run_e2e_tests() {
    log_info "=== Running integration tests (pytest tests/integration/) ==="

    # Set environment variables (mirrors what CI does)
    export APM_E2E_TESTS="1"
    if [[ -n "${APM_RUN_INTEGRATION_TESTS:-}" ]]; then
        export APM_RUN_INTEGRATION_TESTS
    fi
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        export GITHUB_TOKEN="$GITHUB_TOKEN"
    fi

    log_info "Environment:"
    echo "  APM_E2E_TESTS: $APM_E2E_TESTS"
    if [[ -n "${APM_RUN_INTEGRATION_TESTS:-}" ]]; then
        echo "  APM_RUN_INTEGRATION_TESTS: $APM_RUN_INTEGRATION_TESTS"
    else
        echo "  APM_RUN_INTEGRATION_TESTS: (not set; network-integration tests will be skipped)"
    fi
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        echo "  GITHUB_TOKEN: (set)"
    else
        echo "  GITHUB_TOKEN: (not set)"
    fi
    if [[ -n "${GITHUB_APM_PAT:-}" ]]; then
        echo "  GITHUB_APM_PAT: (set)"
    else
        echo "  GITHUB_APM_PAT: (not set)"
    fi
    if [[ -n "${ADO_APM_PAT:-}" ]]; then
        echo "  ADO_APM_PAT: (set)"
    else
        echo "  ADO_APM_PAT: (not set)"
    fi
    echo "  PATH contains: $(dirname "$(which apm)")"
    echo "  APM binary: $(which apm)"
    echo "  APM_BINARY_PATH: ${APM_BINARY_PATH:-(unset)}"

    # Activate virtual environment if it exists
    if [[ -f ".venv/bin/activate" ]]; then
        source .venv/bin/activate
    fi

    log_info "Invoking pytest tests/integration/ (marker registry handles per-test gating)"
    # Allow CI to pass extra pytest args (sharding, xdist) via the
    # PYTEST_EXTRA_ARGS env var. Empty by default for local runs.
    # shellcheck disable=SC2206
    extra_args=(${PYTEST_EXTRA_ARGS:-})
    if pytest tests/integration/ -v --tb=short "${extra_args[@]}"; then
        log_success "Integration test suite passed (collected and ran via pytest discovery)"
    else
        log_error "Integration test suite reported failures"
        exit 1
    fi
}

# Main execution
main() {
    echo "APM CLI Integration Testing - Unified CI/Local Script"
    echo "====================================================="
    echo ""
    echo "This script adapts to CI (using artifacts) or local (building) environments."
    echo "Resolves tokens, builds/locates the apm binary, sets up runtimes, then invokes pytest tests/integration/ once."
    echo ""
    
    check_prerequisites
    detect_platform
    detect_environment
    build_binary
    setup_binary_for_testing
    setup_runtimes
    install_test_dependencies
    run_e2e_tests
    
    log_success "All integration tests completed successfully!"
    echo ""
    if [[ "$USE_EXISTING_BINARY" == "true" ]]; then
        echo "CI mode: Used pre-built artifacts and validated integration workflow"
    else
        echo "Local mode: Built binary and validated full integration process"
    fi
    echo ""
    log_success "Ready for release validation!"
}

# Cleanup on exit
cleanup() {
    if [[ -f "apm" ]]; then
        rm -f apm
    fi
}
trap cleanup EXIT

# Run main function
main "$@"
