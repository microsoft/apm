#!/bin/bash
# Sourced by per-distro scenario scripts after `source dev-container-features-test-lib`.
# Provides the four checks that must pass on every image.

check "apm binary is on PATH" \
    command -v apm

check "apm --version exits cleanly" \
    apm --version

check "apm --version outputs a semver string" \
    bash -c "apm --version | grep -E '[0-9]+\.[0-9]+\.[0-9]+'"

check "apm --help exits cleanly" \
    apm --help
