#!/usr/bin/env bash
# Anti-regression lint: prevent the bug class behind #1212 from recurring.
#
# The bug class: PAT->AAD bearer fallback protocol open-coded across multiple
# call sites that drift over time. The canonical helper is
# `AuthResolver.try_with_fallback` in src/apm_cli/core/auth.py.
#
# Two rules:
#   A. `get_bearer_provider` must only be referenced inside the auth boundary
#      (core/auth.py, core/auth_chain.py, core/azure_cli.py) or tests. Other
#      callers must route through the AuthResolver fallback chain.
#   B. Git network subprocess and HTTP egress credential routing is enforced
#      by the AST architecture linter (scripts/lint-architecture-boundaries.sh).
#      This entrypoint only verifies the deferred guards stay registered on an
#      owner record, so the deferral cannot lapse silently.
#
# Run from repo root:  bash scripts/lint-auth-signals.sh
# Exits non-zero on any violation. Designed to be hooked into CI Lint job.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

violations=0

# --- Rule A -----------------------------------------------------------------
# Allowed: src/apm_cli/core/auth.py, src/apm_cli/core/auth_chain.py,
# src/apm_cli/core/azure_cli.py, tests/**.
# All other src/ files referencing get_bearer_provider must go through the
# canonical helper instead. We scan for ANY reference to the symbol (not
# just imports), so multi-line `from ... import (...)` blocks and
# module-attribute access (e.g. `azure_cli.get_bearer_provider`) are
# caught equally.
echo "[*] Rule A: get_bearer_provider boundary (any reference)"
rule_a_hits=$(
    grep -rEn '\bget_bearer_provider\b' \
        src/apm_cli/ --include='*.py' \
        | grep -vE '(src/apm_cli/core/auth\.py|src/apm_cli/core/auth_chain\.py|src/apm_cli/core/azure_cli\.py)' \
        || true
)

while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    echo "  [x] $hit"
    echo "      get_bearer_provider must be routed through"
    echo "      the AuthResolver fallback chain (core/auth.py)."
    violations=$((violations + 1))
done <<EOF
$rule_a_hits
EOF

# --- Rule B -----------------------------------------------------------------
# Deferred to the AST architecture linter, which CI runs separately via
# scripts/lint-architecture-boundaries.sh. A guard listed here must be declared
# on an owner record in .apm/architecture/owners/.
echo "[*] Rule B: credential routing deferred to the architecture linter"
rule_b_guards="transport-platform-auth-outcome-owner transport-platform-authenticated-egress transport-platform-authenticated-egress-credential-headers transport-platform-git-child-environment"
for guard in $rule_b_guards; do
    if ! grep -rqF "\"$guard\"" .apm/architecture/owners/; then
        echo "  [x] deferred guard '$guard' is not registered on any owner record"
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo
    echo "[x] $violations auth-protocol violation(s). See above."
    echo "    Background: #1212 bug class -- duplicated PAT->bearer protocol."
    exit 1
fi

echo "[+] auth-signal lint clean"
