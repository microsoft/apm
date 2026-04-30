#!/bin/bash
set -e

# Load the devcontainer test helper (injected by `devcontainer features test`)
# shellcheck source=/dev/null
source dev-container-features-test-lib

# Source generic checks (applies to all scenarios)
# shellcheck source=/dev/null
source "$(dirname "$0")/generic-checks.sh"

# Scenario-specific checks
check "apm --version reports the pinned version (0.8.11)" \
    bash -c "apm --version | grep -q '0\.8\.11'"

reportResults
