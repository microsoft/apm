#!/usr/bin/env bash
# verify-lint-mirror.sh -- run the CI Lint job mirror locally.
#
# Mirrors `.apm/instructions/linting.instructions.md` verbatim.
# Stops at the first failure. Diagnostics on stderr; structured
# pass/fail summary on stdout.
#
# Exit codes:
#   0 all checks passed
#   1 one or more checks failed
#   2 usage error / missing dependency
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: verify-lint-mirror.sh [--help]

Run the four CI-mirror lint steps from
.apm/instructions/linting.instructions.md verbatim:

  1. ruff check (style + imports + lint)
  2. ruff format --check
  3. pylint R0801 duplication guard (min-similarity-lines=10)
  4. auth-signals boundary lint (scripts/lint-auth-signals.sh)

Stops at first failure. Stdout: one PASS/FAIL line per step plus
final summary. Stderr: tool diagnostics.

No arguments. Run from the worktree root.

Note: the YAML I/O guard, file-length guardrail, and
`relative_to` guard from the CI job are pure-grep one-liners that
this mirror does NOT run -- they are cheap to invoke directly and
were skipped to keep this script focused. Run them manually if you
touched those surfaces.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

if [[ $# -gt 0 ]]; then
    echo "[x] unexpected arguments: $*" >&2
    usage >&2
    exit 2
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "[x] missing required tool: uv" >&2
    exit 2
fi

# Track step status. STEP_STATUS holds "PASS" or "FAIL <msg>".
declare -a STEP_NAMES=(
    "ruff check"
    "ruff format --check"
    "pylint R0801 duplication"
    "auth-signals boundary"
)
declare -a STEP_CMDS=(
    "uv run --extra dev ruff check src/ tests/"
    "uv run --extra dev ruff format --check src/ tests/"
    "uv run --extra dev python -m pylint --disable=all --enable=R0801 --min-similarity-lines=10 --fail-on=R0801 src/apm_cli/"
    "bash scripts/lint-auth-signals.sh"
)
declare -a STEP_STATUS=()

OVERALL=0
for i in "${!STEP_CMDS[@]}"; do
    NAME="${STEP_NAMES[$i]}"
    CMD="${STEP_CMDS[$i]}"
    echo "[*] $NAME" >&2
    if eval "$CMD" >&2; then
        STEP_STATUS+=("PASS")
        echo "[+] $NAME" >&2
    else
        STEP_STATUS+=("FAIL")
        echo "[x] $NAME -- see diagnostics above" >&2
        OVERALL=1
        break
    fi
done

echo
echo "=== verify-lint-mirror summary ==="
for i in "${!STEP_NAMES[@]}"; do
    STATUS="${STEP_STATUS[$i]:-SKIPPED}"
    printf "  %-30s %s\n" "${STEP_NAMES[$i]}" "$STATUS"
done

if [[ $OVERALL -eq 0 ]]; then
    echo "[+] all lint-mirror checks PASSED"
else
    echo "[x] lint-mirror FAILED -- fix and re-run before pushing"
fi
exit $OVERALL
