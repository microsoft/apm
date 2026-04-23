#!/bin/bash
set -e

source dev-container-features-test-lib
# shellcheck source=generic-checks.sh
source "$(dirname "$0")/generic-checks.sh"

# -- Debian-specific: confirm apt-get path was exercised ----------------------

check "apt-get is the system package manager" \
    command -v apt-get

check "python3 is installed via apt" \
    bash -c "dpkg -l python3 | grep -q '^ii'"

check "git is installed via apt" \
    bash -c "dpkg -l git | grep -q '^ii'"

# -- Report --------------------------------------------------------------------
reportResults
