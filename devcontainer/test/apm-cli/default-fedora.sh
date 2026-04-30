#!/bin/bash
set -e

source dev-container-features-test-lib
# shellcheck source=generic-checks.sh
source "$(dirname "$0")/generic-checks.sh"

# --- Fedora-specific: confirm dnf path was exercised ---

check "dnf is the system package manager" \
    command -v dnf

check "python3 is installed via dnf" \
    bash -c "rpm -q python3 >/dev/null"

check "git is installed via dnf" \
    bash -c "rpm -q git >/dev/null"

# Report ------------------------------------------------------
reportResults
