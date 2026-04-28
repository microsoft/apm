#!/bin/bash
set -e

source dev-container-features-test-lib
# shellcheck source=generic-checks.sh
source "$(dirname "$0")/generic-checks.sh"

# -- Ubuntu 24.04 specific: PEP 668 distro --------------------------------------

check "Running on Ubuntu 24.04 (PEP 668 distro)" \
    bash -c "grep -q 'PRETTY_NAME=\"Ubuntu 24.04' /etc/os-release"

check "apm-cli is visible to system pip (PEP 668 --break-system-packages succeeded)" \
    bash -c "pip3 show apm-cli | grep -q 'Location:'"

# -- Report --------------------------------------------------------------------
reportResults
