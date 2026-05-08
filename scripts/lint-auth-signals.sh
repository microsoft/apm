#!/usr/bin/env bash
# Anti-regression lint: prevent the bug class behind #1212 from recurring.
#
# The bug class: PAT->AAD bearer fallback protocol open-coded across multiple
# call sites that drift over time. The canonical helper is
# `AuthResolver.execute_with_bearer_fallback` in src/apm_cli/core/auth.py.
#
# Two rules:
#   A. `get_bearer_provider` must only be imported inside the auth boundary
#      (core/auth.py, core/azure_cli.py) or tests. Other callers must route
#      through `execute_with_bearer_fallback`.
#   B. Raw `git ls-remote` subprocess invocations against ADO must either
#      live inside core/auth.py (the helper itself) or carry an explicit
#      `# auth-delegated:` annotation pointing at the route.
#
# Run from repo root:  bash scripts/lint-auth-signals.sh
# Exits non-zero on any violation. Designed to be hooked into CI Lint job.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

violations=0

# --- Rule A -----------------------------------------------------------------
# Allowed: src/apm_cli/core/auth.py, src/apm_cli/core/azure_cli.py, tests/**.
# All other src/ files importing get_bearer_provider must go through the
# canonical helper instead.
echo "[*] Rule A: get_bearer_provider import boundary"
rule_a_hits=$(
    grep -rEn "(from .*azure_cli import|import).*get_bearer_provider" \
        src/apm_cli/ --include='*.py' \
        | grep -vE '(src/apm_cli/core/auth\.py|src/apm_cli/core/azure_cli\.py)' \
        || true
)

# Exempt sites (tracked here, not via inline annotations, so the boundary is
# auditable in one place).
rule_a_exempt="src/apm_cli/install/validation.py"

while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    file="${hit%%:*}"
    exempt=0
    for e in $rule_a_exempt; do
        if [ "$file" = "$e" ]; then
            exempt=1
            break
        fi
    done
    if [ $exempt -eq 0 ]; then
        echo "  [x] $hit"
        echo "      get_bearer_provider must be routed through"
        echo "      AuthResolver.execute_with_bearer_fallback (auth.py)."
        violations=$((violations + 1))
    fi
done <<EOF
$rule_a_hits
EOF

# --- Rule B -----------------------------------------------------------------
# Raw `git ls-remote` against ADO must either be inside auth.py OR carry the
# `# auth-delegated:` marker. The marker forces authors to think about where
# the bearer fallback is wired before adding a new site.
echo "[*] Rule B: git ls-remote auth-delegated annotation"
rule_b_hits=$(
    grep -rEn '"ls-remote"' src/apm_cli/ --include='*.py' \
        | grep -vE 'src/apm_cli/core/auth\.py' \
        || true
)
while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    file="${hit%%:*}"
    rest="${hit#*:}"
    line="${rest%%:*}"
    start=$((line > 5 ? line - 5 : 1))
    end=$((line + 5))
    window=$(sed -n "${start},${end}p" "$file")
    if echo "$window" | grep -qE '(auth-delegated|execute_with_bearer_fallback)'; then
        continue
    fi
    case "$file" in
        src/apm_cli/marketplace/ref_resolver.py) continue ;;
        src/apm_cli/commands/marketplace/doctor.py) continue ;;
        src/apm_cli/install/validation.py) continue ;;
        src/apm_cli/marketplace/git_stderr.py) continue ;;  # docstring example, not a call
    esac
    echo "  [x] $hit"
    echo "      git ls-remote outside core/auth.py needs:"
    echo "      (a) call to execute_with_bearer_fallback nearby, or"
    echo "      (b) a '# auth-delegated: <reason>' comment within 5 lines."
    violations=$((violations + 1))
done <<EOF
$rule_b_hits
EOF

if [[ $violations -gt 0 ]]; then
    echo
    echo "[x] $violations auth-protocol violation(s). See above."
    echo "    Background: #1212 bug class -- duplicated PAT->bearer protocol."
    exit 1
fi

echo "[+] auth-signal lint clean"
