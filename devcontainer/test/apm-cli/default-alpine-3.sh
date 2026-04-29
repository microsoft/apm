#!/bin/bash
set -e

source dev-container-features-test-lib
# shellcheck source=generic-checks.sh
source "$(dirname "$0")/generic-checks.sh"

# -- Alpine-specific: confirm apk path was exercised --------------------------

check "apk is the system package manager" \
    command -v apk

check "python3 is installed via apk" \
    bash -c "apk info python3 | grep -q python3"

check "git is installed via apk" \
    bash -c "apk info git | grep -q git"

# -- Report --------------------------------------------------------------------
reportResults
