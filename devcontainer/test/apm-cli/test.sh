#!/bin/bash
set -e

# Load the devcontainer test helper (injected by `devcontainer features test`)
# shellcheck source=/dev/null
source dev-container-features-test-lib

# Source generic checks (applies to all distros)
# shellcheck source=/dev/null
source "$(dirname "$0")/generic-checks.sh"

reportResults
