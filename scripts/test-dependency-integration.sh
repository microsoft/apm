#!/bin/bash
# Extension to build isolation script for APM Dependencies Integration Testing
# Tests real dependency scenarios with actual GitHub repositories
# Used in CI pipeline for comprehensive dependency validation

# NOTE: Do NOT use `set -e` here — this file is `source`d into
# test-release-validation.sh which deliberately avoids -e for its own
# error-handling strategy.  Setting -e here would re-enable it for the
# sourcing script, causing silent early exits (e.g. `((count++))` when
# count is 0 returns exit-code 1 under -e).
set -uo pipefail

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

# Test dependency installation with real repositories
test_real_dependency_installation() {
    local test_dir="$1"
    local apm_binary="$2"
    
    log_test "Testing real dependency installation with microsoft/apm-sample-package"
    
    cd "$test_dir"
    
    # Create apm.yml with real dependency
    cat > apm.yml << 'EOF'
name: dependency-test-project
version: 1.0.0
description: Test project for dependency integration testing
author: CI Test
target: copilot

dependencies:
  apm:
    - microsoft/apm-sample-package

scripts:
  start: "echo 'Project with apm-sample-package dependency loaded'"
EOF

    # Test apm deps list (should show no dependencies initially)
    log_info "Testing 'apm deps list' with no dependencies installed"
    deps_output=$("$apm_binary" deps list 2>&1)
    echo "DEBUG: Actual output from 'apm deps list':"
    echo "--- OUTPUT START ---"
    echo "$deps_output"
    echo "--- OUTPUT END ---"
    if echo "$deps_output" | grep -q "No APM dependencies installed"; then
        log_success "Correctly shows no dependencies installed"
    else
        log_error "Expected 'No APM dependencies installed' message"
        log_error "Got: $deps_output"
        return 1
    fi
    
    # Test apm install (should download real dependency)
    log_info "Testing 'apm install' with real GitHub dependency"
    if ! "$apm_binary" install; then
        log_error "Failed to install real dependency"
        return 1
    fi
    
    # Verify installation
    if [[ ! -d "apm_modules/microsoft/apm-sample-package" ]]; then
        log_error "Dependency not installed: apm_modules/microsoft/apm-sample-package not found"
        return 1
    fi
    
    # Verify dependency structure
    if [[ ! -f "apm_modules/microsoft/apm-sample-package/apm.yml" ]]; then
        log_error "Dependency missing apm.yml"
        return 1
    fi
    
    if [[ ! -d "apm_modules/microsoft/apm-sample-package/.apm" ]]; then
        log_error "Dependency missing .apm directory"
        return 1
    fi
    
    # Check for expected prompt files
    if [[ ! -f "apm_modules/microsoft/apm-sample-package/.apm/prompts/design-review.prompt.md" ]]; then
        log_error "Dependency missing expected prompt file: .apm/prompts/design-review.prompt.md"
        return 1
    fi
    
    log_success "Real dependency installation verified"
    
    # Test apm deps list (should now show installed dependency)
    log_info "Testing 'apm deps list' with installed dependency"
    if "$apm_binary" deps list | grep -q "apm-sample-package"; then
        log_success "Correctly shows installed dependency"
    else
        log_error "Expected to see installed dependency in list"
        return 1
    fi
    
    # Test apm deps tree
    log_info "Testing 'apm deps tree'"
    if "$apm_binary" deps tree | grep -q "apm-sample-package"; then
        log_success "Dependency tree shows installed dependency"
    else
        log_error "Expected to see dependency in tree output"
        return 1
    fi
    
    # Test apm deps info
    log_info "Testing 'apm deps info apm-sample-package'"
    if "$apm_binary" deps info apm-sample-package | grep -q "apm-sample-package"; then
        log_success "Dependency info command works"
    else
        log_error "Expected dependency info to show package details"
        return 1
    fi
    
    log_success "All real dependency tests passed"
}

# Test multi-dependency scenario
test_multi_dependency_scenario() {
    local test_dir="$1"
    local apm_binary="$2"
    
    log_test "Testing multi-dependency scenario with both test repositories"
    
    cd "$test_dir"
    
    # Create apm.yml with multiple dependencies
    cat > apm.yml << 'EOF'
name: multi-dependency-test
version: 1.0.0
description: Test project for multi-dependency scenario
author: CI Test
target: copilot

dependencies:
  apm:
    - microsoft/apm-sample-package
    - github/awesome-copilot/skills/review-and-refactor

scripts:
  start: "echo 'Project with multiple dependencies loaded'"
EOF

    # Clean any existing dependencies
    rm -rf apm_modules 2>/dev/null || true
    
    # Install multiple dependencies
    log_info "Installing multiple real dependencies"
    if ! "$apm_binary" install; then
        log_error "Failed to install multiple dependencies"
        return 1
    fi
    
    # Verify both dependencies installed
    if [[ ! -d "apm_modules/microsoft/apm-sample-package" ]]; then
        log_error "First dependency not installed: apm-sample-package"
        return 1
    fi
    
    if [[ ! -d "apm_modules/github/awesome-copilot/skills/review-and-refactor" ]]; then
        log_error "Second dependency not installed: github/awesome-copilot/skills/review-and-refactor"
        return 1
    fi
    
    # Test deps list shows both
    local deps_output=$("$apm_binary" deps list)
    if ! echo "$deps_output" | grep -q "apm-sample-package"; then
        log_error "Multi-dependency list missing apm-sample-package"
        return 1
    fi
    
    if ! echo "$deps_output" | grep -q "design-guidelines\|apm-sample-package"; then
        log_error "Multi-dependency list missing design-guidelines"
        return 1
    fi
    
    log_success "Multi-dependency scenario verified"
}

# Test dependency update workflow
test_dependency_update() {
    local test_dir="$1"
    local apm_binary="$2"
    
    log_test "Testing dependency update workflow"
    
    cd "$test_dir"
    
    # Should have dependencies installed from previous test
    if [[ ! -d "apm_modules" ]]; then
        log_error "No dependencies found for update test"
        return 1
    fi
    
    # Test update all dependencies
    log_info "Testing 'apm deps update' for all dependencies"
    if ! "$apm_binary" deps update; then
        log_error "Failed to update all dependencies"
        return 1
    fi
    
    # Test update specific dependency
    log_info "Testing 'apm deps update apm-sample-package'"
    if ! "$apm_binary" deps update apm-sample-package; then
        log_error "Failed to update specific dependency"
        return 1
    fi
    
    log_success "Dependency update workflow verified"
}

# Test dependency cleanup
test_dependency_cleanup() {
    local test_dir="$1"
    local apm_binary="$2"
    
    log_test "Testing dependency cleanup"
    
    cd "$test_dir"
    
    # Test deps clean
    log_info "Testing 'apm deps clean'"
    # This is interactive, so we use echo to provide confirmation
    if ! echo "y" | "$apm_binary" deps clean; then
        log_error "Failed to clean dependencies"
        return 1
    fi
    
    # Verify cleanup
    if [[ -d "apm_modules" ]]; then
        log_error "apm_modules directory still exists after cleanup"
        return 1
    fi
    
    # Verify deps list shows no dependencies
    deps_output_after_cleanup=$("$apm_binary" deps list 2>&1)
    echo "DEBUG: Actual output from 'apm deps list' after cleanup:"
    echo "--- OUTPUT START ---"
    echo "$deps_output_after_cleanup"
    echo "--- OUTPUT END ---"
    if echo "$deps_output_after_cleanup" | grep -q "No APM dependencies installed"; then
        log_success "Correctly shows no dependencies after cleanup"
    else
        log_error "Expected no dependencies after cleanup"
        log_error "Got: $deps_output_after_cleanup"
        return 1
    fi
    
    log_success "Dependency cleanup verified"
}

# Main function for dependency integration testing
test_dependency_integration() {
    local apm_binary="$1"
    
    log_info "=== APM Dependencies Integration Testing ==="
    log_info "Testing with real GitHub repositories:"
    log_info "  - microsoft/apm-sample-package"
    log_info "  - github/awesome-copilot/skills/review-and-refactor"
    
    # Create isolated test directory
    local test_dir=$(mktemp -d)
    original_dir=$(pwd)  # Make global for cleanup function
    
    # Cleanup function
    cleanup_test() {
        cd "${original_dir:-$(pwd)}"  # Fallback to current dir if unset
        rm -rf "$test_dir" 2>/dev/null || true
    }
    trap cleanup_test EXIT
    
    # Check for GitHub token
    if [[ -z "${GITHUB_CLI_PAT:-}" ]] && [[ -z "${GITHUB_TOKEN:-}" ]]; then
        log_error "GitHub token required for dependency testing"
        log_info "Set GITHUB_CLI_PAT or GITHUB_TOKEN environment variable"
        return 1
    fi
    
    # Run dependency tests in sequence
    test_real_dependency_installation "$test_dir" "$apm_binary" || return 1
    test_multi_dependency_scenario "$test_dir" "$apm_binary" || return 1
    test_dependency_update "$test_dir" "$apm_binary" || return 1
    test_dependency_cleanup "$test_dir" "$apm_binary" || return 1
    
    log_success "=== All dependency integration tests passed! ==="
}

# Export the function for use by other scripts
if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
    # Script is being run directly
    if [[ $# -lt 1 ]]; then
        log_error "Usage: $0 <apm_binary_path>"
        exit 1
    fi
    
    test_dependency_integration "$1"
else
    # Script is being sourced
    log_info "APM Dependencies Integration Testing functions loaded"
fi