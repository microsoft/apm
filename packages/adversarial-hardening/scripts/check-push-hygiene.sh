#!/usr/bin/env bash
# ASCII-only. Pillar B: push-hygiene gate for a candidate hardening fold.
#
# Enforces three charter invariants AFTER a shepherd-driver fold and
# BEFORE the commit is accepted (genesis S7 deterministic verifier):
#
#   1. CI-COLLECTED (PILL-B1): every new/changed test file in the fold
#      diff is COLLECTED by the merge-queue lane CI actually uses. A
#      green-in-some-lane test that the merge_group lane never collects
#      is the 25k-uncollected-test trap -> REJECT.
#   2. DIFF-MINIMAL (PILL-B2): the fold diff carries no orphaned
#      fixtures, scratch/debug files, or scaffolding under the test
#      root that no collected test references -> REJECT.
#   3. RIGHT-ALTITUDE (PILL-B3): a fold that touches more than one
#      source module ships an INTEGRATION test, not only a unit test
#      -> REJECT (silent cross-module drift risk).
#
# The gate is READ-ONLY: it never writes the head. On REJECT it exits
# non-zero so the orchestrator's A11 reconciliation loop branches back
# to shepherd-driver instead of advancing the finding to terminal.
#
# Non-interactive. Structured JSON on stdout; diagnostics on stderr.
# Exit codes: 0 = pass, 1 = reject, 2 = runner error.
#
# Shelled tools are version-pinned by routing through `uv run` (the
# repo's locked environment) for pytest; git is used read-only.

set -u

PROG="check-push-hygiene.sh"

# -------- defaults (override via flags) -------------------------------
BASE_REF="origin/main"
TEST_ROOT="tests"
SRC_ROOT="src"
# The collect command MUST mirror the merge-queue lane. Default routes
# through uv run so the pinned pytest is used. The orchestrator should
# pass the exact merge_group lane command when it differs.
COLLECT_CMD="uv run --extra dev pytest --collect-only -q"
# A path is an integration test if it matches this extended-regex.
INTEGRATION_RE='(integration|/it_|_it\.py|tests/integration/)'

usage() {
  cat <<'EOF'
check-push-hygiene.sh - Pillar B push-hygiene gate (read-only).

USAGE:
  check-push-hygiene.sh [--base REF] [--test-root DIR] [--src-root DIR]
                        [--collect-cmd "CMD"] [--integration-re REGEX]
                        [--help]

OPTIONS:
  --base REF           Diff base to compute the fold's changed files
                       (default: origin/main). The diff is REF...HEAD.
  --test-root DIR      Root under which tests live (default: tests).
  --src-root DIR       Root under which source modules live (default: src).
  --collect-cmd "CMD"  Command that lists collected test nodeids the way
                       the MERGE-QUEUE lane does. Must emit nodeids on
                       stdout. Default: "uv run --extra dev pytest
                       --collect-only -q". Pass the merge_group lane's
                       exact command when it differs.
  --integration-re RE  Extended-regex marking a path as an integration
                       test (default covers integration/ and it_ names).
  --help               Print this help and exit 0.

OUTPUT:
  Structured JSON on stdout; human diagnostics on stderr.

EXIT:
  0  all three checks pass (fold is hygienic).
  1  one or more checks REJECT (loop back to shepherd-driver).
  2  runner error (bad args, git/pytest unavailable, collect failed).
EOF
}

log() { printf '%s\n' "$*" >&2; }

# -------- arg parse ---------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE_REF="${2:?--base needs a value}"; shift 2 ;;
    --test-root) TEST_ROOT="${2:?--test-root needs a value}"; shift 2 ;;
    --src-root) SRC_ROOT="${2:?--src-root needs a value}"; shift 2 ;;
    --collect-cmd) COLLECT_CMD="${2:?--collect-cmd needs a value}"; shift 2 ;;
    --integration-re) INTEGRATION_RE="${2:?--integration-re needs a value}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) log "[x] unknown argument: $1"; usage; exit 2 ;;
  esac
done

command -v git >/dev/null 2>&1 || { log "[x] git not found"; exit 2; }

# -------- compute the fold diff --------------------------------------
if ! MERGE_BASE="$(git merge-base "$BASE_REF" HEAD 2>/dev/null)"; then
  # Fall back to the raw base ref if merge-base fails (shallow clone).
  MERGE_BASE="$BASE_REF"
fi

if ! CHANGED="$(git diff --name-only --diff-filter=AM "$MERGE_BASE"...HEAD 2>/dev/null)"; then
  log "[x] git diff failed against base: $MERGE_BASE"
  exit 2
fi

# Partition changed files.
CHANGED_TESTS="$(printf '%s\n' "$CHANGED" | grep -E "^${TEST_ROOT}/.*\.py$" || true)"
CHANGED_TEST_NONPY="$(printf '%s\n' "$CHANGED" | grep -E "^${TEST_ROOT}/" | grep -Ev '\.py$' || true)"
CHANGED_SRC="$(printf '%s\n' "$CHANGED" | grep -E "^${SRC_ROOT}/.*\.py$" || true)"

# -------- collect nodeids the merge-queue way ------------------------
COLLECTED=""
COLLECT_RC=0
if [ -n "$CHANGED_TESTS" ] || [ -n "$CHANGED_TEST_NONPY" ]; then
  log "[>] collecting tests via: $COLLECT_CMD"
  if ! COLLECTED="$($COLLECT_CMD 2>/dev/null)"; then
    COLLECT_RC=$?
    log "[x] collect command failed (rc=$COLLECT_RC): $COLLECT_CMD"
    exit 2
  fi
fi

# -------- check 1: CI-COLLECTED --------------------------------------
UNCOLLECTED=""
for tf in $CHANGED_TESTS; do
  # A test file is collected if any collected nodeid starts with its path.
  if ! printf '%s\n' "$COLLECTED" | grep -qF "$tf"; then
    UNCOLLECTED="${UNCOLLECTED}${tf} "
  fi
done

# -------- check 2: DIFF-MINIMAL (orphan non-test artifacts) ----------
# Any non-.py file added under the test root that no collected nodeid
# and no changed test file references by basename is an orphan.
ORPHANS=""
for art in $CHANGED_TEST_NONPY; do
  base="$(basename "$art")"
  referenced=0
  # Referenced by a collected nodeid?
  if printf '%s\n' "$COLLECTED" | grep -qF "$base"; then referenced=1; fi
  # Referenced by the text of any changed test file?
  if [ "$referenced" -eq 0 ] && [ -n "$CHANGED_TESTS" ]; then
    if git grep -qF "$base" -- $CHANGED_TESTS 2>/dev/null; then referenced=1; fi
  fi
  if [ "$referenced" -eq 0 ]; then ORPHANS="${ORPHANS}${art} "; fi
done

# -------- check 3: RIGHT-ALTITUDE ------------------------------------
# Count distinct source module dirs touched (two path segments under
# the src root, e.g. src/apm_cli/<module>).
MODULES="$(printf '%s\n' "$CHANGED_SRC" \
  | awk -F/ 'NF>=3 {print $1"/"$2"/"$3}' | sort -u || true)"
MODULE_COUNT=0
[ -n "$MODULES" ] && MODULE_COUNT="$(printf '%s\n' "$MODULES" | grep -c . )"

HAS_INTEGRATION=0
if printf '%s\n' "$CHANGED_TESTS" | grep -Eq "$INTEGRATION_RE"; then
  HAS_INTEGRATION=1
fi

ALTITUDE_STATUS="pass"
if [ "$MODULE_COUNT" -gt 1 ] && [ "$HAS_INTEGRATION" -eq 0 ]; then
  ALTITUDE_STATUS="reject"
fi

# -------- verdict -----------------------------------------------------
COLLECTED_STATUS="pass"; [ -n "$UNCOLLECTED" ] && COLLECTED_STATUS="reject"
MINIMAL_STATUS="pass"; [ -n "$ORPHANS" ] && MINIMAL_STATUS="reject"

RESULT="pass"; EXIT=0
if [ "$COLLECTED_STATUS" = "reject" ] || [ "$MINIMAL_STATUS" = "reject" ] \
   || [ "$ALTITUDE_STATUS" = "reject" ]; then
  RESULT="reject"; EXIT=1
fi

# -------- emit JSON ---------------------------------------------------
json_array() {
  # Convert a space-separated list into a JSON string array.
  local first=1; printf '['
  for item in $1; do
    [ "$first" -eq 0 ] && printf ', '
    printf '"%s"' "$item"; first=0
  done
  printf ']'
}

{
  printf '{\n'
  printf '  "schema_version": 1,\n'
  printf '  "result": "%s",\n' "$RESULT"
  printf '  "base": "%s",\n' "$MERGE_BASE"
  printf '  "checks": {\n'
  printf '    "ci_collected": {"status": "%s", "uncollected": %s},\n' \
    "$COLLECTED_STATUS" "$(json_array "$UNCOLLECTED")"
  printf '    "diff_minimal": {"status": "%s", "orphans": %s},\n' \
    "$MINIMAL_STATUS" "$(json_array "$ORPHANS")"
  printf '    "right_altitude": {"status": "%s", "modules_touched": %s, "has_integration_test": %s}\n' \
    "$ALTITUDE_STATUS" "$(json_array "$MODULES")" \
    "$([ "$HAS_INTEGRATION" -eq 1 ] && echo true || echo false)"
  printf '  }\n'
  printf '}\n'
}

exit "$EXIT"
