#!/bin/bash
set -e

# Load the devcontainer test helper (injected by `devcontainer features test`)
# shellcheck source=/dev/null
source dev-container-features-test-lib

# ── Tests ────────────────────────────────────────────────────────────────────

check "python3 is on PATH" \
    command -v python3

check "python3 is version 3.12 (from Python devcontainer feature)" \
    bash -c "python3 --version | grep -q 'Python 3.12'"

check "python3 resolves to Python feature install path" \
    bash -c "which python3 | grep -q '/usr/local/python/current/bin/python3'"

check "apm --version exits cleanly" \
    apm --version

# ── Report ────────────────────────────────────────────────────────────────────
reportResults
