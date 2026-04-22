#!/bin/bash
set -e

# Load the devcontainer test helper (injected by `devcontainer features test`)
# shellcheck source=/dev/null
source dev-container-features-test-lib

# ── Tests ────────────────────────────────────────────────────────────────────

check "apm binary is on PATH" \
    command -v apm

check "apm --version exits cleanly" \
    apm --version

check "apm --version outputs a semver string" \
    bash -c "apm --version | grep -E '[0-9]+\.[0-9]+\.[0-9]+'"

check "apm --help exits cleanly" \
    apm --help

# ── Report ────────────────────────────────────────────────────────────────────
reportResults
