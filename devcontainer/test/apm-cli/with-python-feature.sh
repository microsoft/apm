#!/bin/bash
set -e

# Load the devcontainer test helper (injected by `devcontainer features test`)
# shellcheck source=/dev/null
source dev-container-features-test-lib

# Source generic checks (applies to all scenarios)
# shellcheck source=/dev/null
source "$(dirname "$0")/generic-checks.sh"

# Scenario-specific checks
check "python3 is on PATH" \
    command -v python3

check "python3 meets minimum version (3.10+)" \
    bash -c "python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'"

reportResults
