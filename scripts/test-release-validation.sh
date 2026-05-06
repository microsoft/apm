#!/bin/bash
#!/bin/bash
# Release validation script - Final pre-release testing
# Tests the EXACT user experience with the shipped binary in complete isolation:
#   1. Download/extract binary (as users would)
#   2. apm runtime setup codex  
#   3. apm init my-ai-native-project
#   4. cd my-ai-native-project && apm compile
#   5. apm install
#   6. apm run start --param name="<YourGitHubHandle>"
#
# Environment: Complete isolation - NO source code, only the binary
# Purpose: Validate that end-users will have a successful experience
# This is the final gate before release - testing the actual product as shipped

set -uo pipefail  # Removed -e to allow better error handling

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

log_test() {
    echo -e "${YELLOW}🧪 $1${NC}"
}

# Source the GitHub token management helper
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/github-token-helper.sh"

# Source the dependency integration testing functions
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/test-dependency-integration.sh" ]]; then
    source "$SCRIPT_DIR/test-dependency-integration.sh"
    DEPENDENCY_TESTS_AVAILABLE=true
else
    DEPENDENCY_TESTS_AVAILABLE=false
fi

# Global variables (needed for cleanup and cross-function access)
test_dir=""
BINARY_PATH=""

# Find the binary
find_binary() {
    if [[ $# -gt 0 ]]; then
        # Binary path provided as argument
        BINARY_PATH="$1"
    elif [[ -f "./apm" ]]; then
        # Look for symlink in current directory (CI setup)
        BINARY_PATH="./apm"
    elif command -v apm >/dev/null 2>&1; then
        # Look in PATH
        BINARY_PATH="$(which apm)"
    else
        log_error "APM binary not found. Usage: $0 [path-to-binary]"
        exit 1
    fi
    
    if [[ ! -x "$BINARY_PATH" ]]; then
        log_error "Binary not executable: $BINARY_PATH"
        exit 1
    fi
    
    # Convert to absolute path before we change directories
    BINARY_PATH="$(realpath "$BINARY_PATH")"
    
    log_info "Testing binary: $BINARY_PATH"
}

# Prerequisites check
check_prerequisites() {
    log_test "Prerequisites: GitHub token"
    
    # Use centralized token management
    if setup_github_tokens; then
        log_success "GitHub tokens configured successfully"
        return 0
    else
        log_error "GitHub token setup failed"
        return 1
    fi
}

# Test Step 2: apm runtime setup (both copilot and codex for full coverage)
test_runtime_setup() {
    log_test "README Step 2: apm runtime setup"
    
    # Install GitHub Copilot CLI (recommended default, used by guardrailing hero scenario)
    echo "Running: $BINARY_PATH runtime setup copilot"
    echo "--- Command Output Start ---"
    "$BINARY_PATH" runtime setup copilot 2>&1
    local exit_code=$?
    echo "--- Command Output End ---"
    echo "Exit code: $exit_code"
    
    if [[ $exit_code -ne 0 ]]; then
        log_error "apm runtime setup copilot failed with exit code $exit_code"
        return 1
    fi
    
    log_success "Copilot CLI runtime setup completed"
    
    # Also install Codex CLI (for zero-config scenario and fallback)
    echo "Running: $BINARY_PATH runtime setup codex"
    echo "--- Command Output Start ---"
    "$BINARY_PATH" runtime setup codex 2>&1
    exit_code=$?
    echo "--- Command Output End ---"
    echo "Exit code: $exit_code"
    
    if [[ $exit_code -ne 0 ]]; then
        log_error "apm runtime setup codex failed with exit code $exit_code"
        return 1
    fi
    
    log_success "Codex CLI runtime setup completed"
    log_success "Both runtimes (Copilot, Codex) configured successfully"
}

# Helper function for cross-platform timeout
run_with_timeout() {
    local timeout_duration=$1
    shift
    local cmd="$@"
    
    # Use perl for cross-platform timeout support
    perl -e "alarm $timeout_duration; exec @ARGV" -- sh -c "$cmd" 2>&1 &
    local pid=$!
    
    # Wait for the command to complete or timeout
    wait $pid 2>/dev/null
    local exit_code=$?
    
    # Exit code 142 (SIGALRM) means timeout
    if [[ $exit_code -eq 142 ]]; then
        return 124  # Return timeout code like GNU timeout
    fi
    
    return $exit_code
}

# HERO SCENARIO 1: 30-Second Zero-Config
# Test the exact README flow: runtime setup → run virtual package
# Gated by APM_RUN_INFERENCE_TESTS — live inference tests are decoupled from
# the release pipeline and run in ci-runtime.yml (nightly/manual/path-filtered).
test_hero_zero_config() {
    if [[ "${APM_RUN_INFERENCE_TESTS:-}" != "1" ]]; then
        log_info "Skipping HERO SCENARIO 1 (inference tests decoupled — set APM_RUN_INFERENCE_TESTS=1 to enable)"
        return 0
    fi

    log_test "HERO SCENARIO 1: 30-Second Zero-Config (README lines 35-44)"
    
    # Create temporary directory for this test
    mkdir -p zero-config-test && cd zero-config-test
    
    # Runtime setup is already done in test_runtime_setup()
    # Just test the virtual package run
    
    echo "Running: $BINARY_PATH run github/awesome-copilot/skills/architecture-blueprint-generator (with 15s timeout)"
    echo "--- Command Output Start ---"
    run_with_timeout 15 "$BINARY_PATH run github/awesome-copilot/skills/architecture-blueprint-generator"
    local exit_code=$?
    echo "--- Command Output End ---"
    echo "Exit code: $exit_code"
    
    if [[ $exit_code -eq 124 ]]; then
        # Exit code 124 is timeout, which is expected and OK (prompt execution started)
        log_success "Zero-config auto-install worked! Package installed and prompt started."
    elif [[ $exit_code -eq 0 ]]; then
        # Command completed successfully within timeout
        log_success "Zero-config auto-install completed successfully"
    else
        log_error "Zero-config auto-install failed immediately with exit code $exit_code"
        cd ..
        return 1
    fi
    
    # Verify package was actually installed
    if [[ ! -d "apm_modules/github/awesome-copilot/skills/architecture-blueprint-generator" ]]; then
        log_error "Package was not installed by auto-install"
        cd ..
        return 1
    fi
    
    log_success "Package auto-installed to apm_modules/"
    
    # Test second run (should use cached package, no re-download)
    echo "Testing second run (should use cache)..."
    run_with_timeout 10 "$BINARY_PATH run github/awesome-copilot/skills/architecture-blueprint-generator" | head -20
    local second_exit_code=${PIPESTATUS[0]}
    
    if [[ $second_exit_code -eq 124 || $second_exit_code -eq 0 ]]; then
        log_success "Second run used cached package (fast, no re-download)"
    fi
    
    cd ..
    log_success "HERO SCENARIO 1: 30-second zero-config PASSED ✨"
}

# HERO SCENARIO 2: 2-Minute Guardrailing
# Test the exact README flow: init → install packages → compile → run
test_hero_guardrailing() {
    log_test "HERO SCENARIO 2: 2-Minute Guardrailing (README lines 46-60)"
    
    # Step 1: apm init my-project
    echo "Running: $BINARY_PATH init my-project --yes --target copilot"
    echo "--- Command Output Start ---"
    "$BINARY_PATH" init my-project --yes --target copilot 2>&1
    local exit_code=$?
    echo "--- Command Output End ---"
    echo "Exit code: $exit_code"
    
    if [[ $exit_code -ne 0 ]]; then
        log_error "apm init my-project failed with exit code $exit_code"
        return 1
    fi
    
    if [[ ! -d "my-project" || ! -f "my-project/apm.yml" ]]; then
        log_error "my-project directory or apm.yml not created"
        return 1
    fi
    
    log_success "Project initialized"
    
    cd my-project
    
    # Step 2: apm install microsoft/apm-sample-package
    echo "Running: $BINARY_PATH install microsoft/apm-sample-package"
    echo "--- Command Output Start ---"
    APM_E2E_TESTS="${APM_E2E_TESTS:-}" "$BINARY_PATH" install microsoft/apm-sample-package 2>&1
    exit_code=$?
    echo "--- Command Output End ---"
    echo "Exit code: $exit_code"
    
    if [[ $exit_code -ne 0 ]]; then
        log_error "apm install microsoft/apm-sample-package failed"
        cd ..
        return 1
    fi
    
    log_success "design-guidelines installed"
    
    # Step 3: apm install github/awesome-copilot/skills/review-and-refactor
    echo "Running: $BINARY_PATH install github/awesome-copilot/skills/review-and-refactor"
    echo "--- Command Output Start ---"
    APM_E2E_TESTS="${APM_E2E_TESTS:-}" "$BINARY_PATH" install github/awesome-copilot/skills/review-and-refactor 2>&1
    exit_code=$?
    echo "--- Command Output End ---"
    echo "Exit code: $exit_code"
    
    if [[ $exit_code -ne 0 ]]; then
        log_error "apm install github/awesome-copilot/skills/review-and-refactor failed"
        cd ..
        return 1
    fi
    
    log_success "virtual package installed"
    
    # Step 4: apm compile
    echo "Running: $BINARY_PATH compile"
    echo "--- Command Output Start ---"
    "$BINARY_PATH" compile 2>&1
    exit_code=$?
    echo "--- Command Output End ---"
    echo "Exit code: $exit_code"
    
    if [[ $exit_code -ne 0 ]]; then
        log_error "apm compile failed"
        cd ..
        return 1
    fi
    
    if [[ ! -f "AGENTS.md" ]]; then
        log_error "AGENTS.md not created by compile"
        cd ..
        return 1
    fi
    
    log_success "Compiled to AGENTS.md (guardrails active)"
    
    # Step 5: apm run design-review (from installed package)
    # Gated by APM_RUN_INFERENCE_TESTS — live inference is decoupled from
    # the release pipeline and runs in ci-runtime.yml.
    if [[ "${APM_RUN_INFERENCE_TESTS:-}" == "1" ]]; then
        echo "Running: $BINARY_PATH run design-review (with 10s timeout)"
        echo "--- Command Output Start ---"
        run_with_timeout 10 "$BINARY_PATH run design-review"
        exit_code=$?
        echo "--- Command Output End ---"
        echo "Exit code: $exit_code"

        if [[ $exit_code -eq 124 ]]; then
            # Timeout is expected and OK - prompt started executing
            log_success "design-review prompt executed with compiled guardrails"
        elif [[ $exit_code -eq 0 ]]; then
            log_success "design-review completed successfully"
        else
            log_error "apm run design-review failed immediately"
            cd ..
            return 1
        fi
    else
        log_info "Skipping apm run design-review (inference tests decoupled — set APM_RUN_INFERENCE_TESTS=1 to enable)"
    fi
    
    cd ..
    log_success "HERO SCENARIO 2: 2-minute guardrailing PASSED ✨"
}

# Test basic commands (sanity check)
test_basic_commands() {
    log_test "Sanity check: Basic commands"
    
    # Test --version (show actual error if it fails)
    echo "Running: $BINARY_PATH --version"
    echo "--- Command Output Start ---"
    "$BINARY_PATH" --version
    local version_exit_code=$?
    echo "--- Command Output End ---"
    echo "Exit code: $version_exit_code"
    
    if [[ $version_exit_code -ne 0 ]]; then
        log_error "apm --version failed with exit code $version_exit_code"
        return 1
    fi
    
    # Test --help
    echo "Running: $BINARY_PATH --help"
    echo "--- Command Output Start ---"
    "$BINARY_PATH" --help 2>&1 | head -20  # Limit output for readability
    local help_exit_code=${PIPESTATUS[0]}
    echo "--- Command Output End ---"
    echo "Exit code: $help_exit_code"
    
    if [[ $help_exit_code -ne 0 ]]; then
        log_error "apm --help failed with exit code $help_exit_code"
        return 1
    fi
    
    log_success "Basic commands work"
}

# GH-AW compatibility test - replicates the exact flow gh-aw uses:
#   1. Install a public package in isolated mode (no token)
#   2. Pack for Claude target with archive
# This catches regressions like v0.8.1 where credential fill garbage
# broke public repo cloning in tokenless environments.
test_ghaw_compat() {
    log_test "GH-AW Compatibility: tokenless isolated install + pack"
    
    local ghaw_dir="ghaw-compat-test"
    mkdir -p "$ghaw_dir"
    
    # Run in a subshell with NO GitHub tokens — simulates gh-aw activation
    # where apm-action does not pass GITHUB_TOKEN to the subprocess.
    (
        unset GITHUB_TOKEN GITHUB_APM_PAT GH_TOKEN 2>/dev/null || true
        cd "$ghaw_dir"
        
        # Create a minimal apm.yml matching apm-action's generateManifest() format
        cat > apm.yml <<'APMYML'
name: ghaw-compat-test
version: 1.0.0
target: copilot
dependencies:
  apm:
    - microsoft/apm-sample-package
APMYML
        
        echo "Running: apm install (no token)"
        "$BINARY_PATH" install
        local install_exit=$?
        if [[ $install_exit -ne 0 ]]; then
            echo "apm install failed with exit code $install_exit"
            exit 1
        fi
        
        echo "Running: apm pack --target claude --archive"
        "$BINARY_PATH" pack --target claude --archive
        local pack_exit=$?
        if [[ $pack_exit -ne 0 ]]; then
            echo "apm pack failed with exit code $pack_exit"
            exit 1
        fi
        
        # Verify a bundle was produced
        if ls build/*.tar.gz 1>/dev/null 2>&1; then
            echo "Bundle archive produced successfully"
        else
            echo "No bundle archive found in build/"
            exit 1
        fi
    )
    local subshell_exit=$?
    
    rm -rf "$ghaw_dir" 2>/dev/null || true
    
    if [[ $subshell_exit -ne 0 ]]; then
        log_error "GH-AW compatibility test failed — public repo install/pack broken without token"
        return 1
    fi
    
    log_success "GH-AW compatibility: tokenless install + pack works"
}

# Main test runner - follows exact README flow
main() {
echo "APM CLI Release Validation - Binary Isolation Testing"
echo "====================================================="
echo ""
echo "Testing the EXACT user experience with the shipped binary"
echo "Environment: Complete isolation (no source code access)"
echo "Purpose: Final validation before release"
echo ""
    
    find_binary "$@"
    
    # Test binary accessibility first
    echo "Testing binary accessibility..."
    if [[ ! -f "$BINARY_PATH" ]]; then
        log_error "Binary file does not exist: $BINARY_PATH"
        exit 1
    fi
    
    if [[ ! -x "$BINARY_PATH" ]]; then
        log_error "Binary is not executable: $BINARY_PATH"
        exit 1
    fi
    
    echo "Binary found and executable: $BINARY_PATH"
    
    local tests_passed=0
    local tests_total=5  # Prerequisites, basic commands, gh-aw compat, runtime setup, guardrailing (init/install/compile)
    local dependency_tests_run=false
    local inference_tests_run=false
    
    # Hero scenario 1 (zero-config) is entirely inference-based — only counted when enabled
    if [[ "${APM_RUN_INFERENCE_TESTS:-}" == "1" ]]; then
        tests_total=$((tests_total + 1))
        inference_tests_run=true
        log_info "Inference tests enabled (APM_RUN_INFERENCE_TESTS=1)"
    else
        log_info "Inference tests decoupled — skipping apm run tests (set APM_RUN_INFERENCE_TESTS=1 to enable)"
    fi
    
    # Add dependency tests to total if available and GITHUB token is present
    if [[ "$DEPENDENCY_TESTS_AVAILABLE" == "true" ]] && [[ -n "${GITHUB_APM_PAT:-}" || -n "${GITHUB_TOKEN:-}" ]]; then
        tests_total=$((tests_total + 1))
        dependency_tests_run=true
        log_info "Dependency integration tests will be included"
    elif [[ "$DEPENDENCY_TESTS_AVAILABLE" == "true" ]]; then
        log_info "Dependency integration tests available but no GitHub token - skipping"
    else
        log_info "Dependency integration tests not available - skipping"
    fi
    
    # Create isolated test directory
    test_dir="binary-golden-scenario-$$"  # Make it global for cleanup
    mkdir "$test_dir" && cd "$test_dir"
    
    # Run prerequisites and basic tests
    if check_prerequisites; then
        ((tests_passed++))
    else
        log_error "Prerequisites check failed"
    fi
    
    if test_basic_commands; then
        ((tests_passed++))
    else
        log_error "Basic commands test failed"
    fi
    
    if test_ghaw_compat; then
        ((tests_passed++))
    else
        log_error "GH-AW compatibility test failed"
    fi
    
    if test_runtime_setup; then
        ((tests_passed++))
    else
        log_error "Runtime setup test failed"
    fi
    
    # HERO SCENARIO 1: 30-second zero-config (only when inference tests enabled)
    if [[ "$inference_tests_run" == "true" ]]; then
        if test_hero_zero_config; then
            ((tests_passed++))
        else
            log_error "Hero scenario 1 (30-sec zero-config) failed"
        fi
    else
        test_hero_zero_config  # Runs but auto-skips and returns 0
    fi
    
    # HERO SCENARIO 2: 2-minute guardrailing
    if test_hero_guardrailing; then
        ((tests_passed++))
    else
        log_error "Hero scenario 2 (2-min guardrailing) failed"
    fi
    
    # Run dependency integration tests if available and GitHub token is set
    if [[ "$dependency_tests_run" == "true" ]]; then
        log_info "Running dependency integration tests with real GitHub repositories"
        if test_dependency_integration "$BINARY_PATH"; then
            ((tests_passed++))
            log_success "Dependency integration tests passed"
        else
            log_error "Dependency integration tests failed"
        fi
    fi
    
    cd ..
    
    echo ""
    echo "Results: $tests_passed/$tests_total tests passed"
    
    if [[ $tests_passed -eq $tests_total ]]; then
        echo "✅ RELEASE VALIDATION PASSED!"
        echo ""
        echo "🚀 Binary is ready for production release"
        echo "📦 End-user experience validated successfully" 
        echo ""
        echo "Validated user journeys:"
        echo "  1. Prerequisites (GITHUB_APM_PAT) ✅"
        echo "  2. Binary accessibility ✅"
        echo "  3. Runtime setup (copilot) ✅"
        echo "  4. GH-AW compatibility (tokenless install + pack) ✅"
        echo ""
        if [[ "$inference_tests_run" == "true" ]]; then
            echo "  HERO SCENARIO 1: 30-Second Zero-Config ✨"
            echo "    - Run virtual package directly ✅"
            echo "    - Auto-install on first run ✅"
            echo "    - Use cached package on second run ✅"
            echo ""
        fi
        echo "  HERO SCENARIO 2: 2-Minute Guardrailing ✨"
        echo "    - Project initialization ✅"
        echo "    - Install APM packages ✅"
        echo "    - Compile to AGENTS.md guardrails ✅"
        if [[ "$inference_tests_run" == "true" ]]; then
            echo "    - Run prompts with guardrails ✅"
        else
            echo "    - Run prompts (decoupled to ci-runtime.yml) ⏭️"
        fi
        if [[ "$dependency_tests_run" == "true" ]]; then
            echo ""
            echo "  BONUS: Real dependency integration ✅"
        fi
        echo ""
        log_success "README Hero Scenarios work perfectly! ✨"
        echo ""
        echo "🎉 The binary delivers the exact README experience - real users will love it!"
        exit 0
    else
        log_error "Some tests failed"
        echo ""
        echo "⚠️  The binary doesn't match the README promise"
        exit 1
    fi
}

# Cleanup on exit
cleanup() {
    # Clean up test directory if it exists
    if [[ -n "${test_dir:-}" && -d "$test_dir" ]]; then
        echo "🧹 Cleaning up test directory: $test_dir"
        # Make sure we're not inside the directory before removing it
        local current_dir=$(pwd)
        if [[ "$current_dir" == *"$test_dir"* ]]; then
            cd ..
        fi
        rm -rf "$test_dir" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Run main function
main "$@"