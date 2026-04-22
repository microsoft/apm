#!/bin/bash
set -e

# Load the devcontainer test helper (injected by `devcontainer features test`)
# shellcheck source=/dev/null
source dev-container-features-test-lib

# ── Tests ────────────────────────────────────────────────────────────────────

check "apm --version outputs exactly 0.8.11" \
    bash -c "apm --version | grep -q '0.8.11'"

# ── Report ────────────────────────────────────────────────────────────────────
reportResults
