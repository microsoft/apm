#!/usr/bin/env bash
# OpenAPM Mode B (silent-extension) detector.
#
# Fires when a PR adds substantive net-new code under a normative
# critical path (see critical_paths.txt) without adding any matching
# spec artifact (anchor in the spec body, row in the requirements
# manifest, or new @pytest.mark.req marker). Complements the 4-way
# orphan_check.py, which only catches divergence among ALREADY-
# DECLARED artifacts.
#
# Bypass: add `apm-spec-waiver: <reason, >= 16 chars>` to the PR body
# or a commit message between merge-base and HEAD.
#
# Exit codes:
#   0 = no fire (in-scope diff is below threshold, spec-concurrent,
#       out-of-scope, or waived)
#   1 = fire (substantive critical-path add, no spec citation, no
#       waiver)
#   2 = internal error (missing files, git failure)

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

PATHS_FILE="tests/spec_conformance/critical_paths.txt"
SPEC_BODY="docs/src/content/docs/specs/openapm-v0.1.md"
SPEC_MANIFEST="docs/public/specs/manifests/openapm-v0.1.requirements.yml"
BASE="${BASE_REF:-origin/main}"
THRESHOLD="${MODE_B_THRESHOLD:-20}"

if [ ! -f "$PATHS_FILE" ]; then
  echo "::error::mode_b: missing $PATHS_FILE" >&2
  exit 2
fi

mapfile_compat() {
  # Portable replacement for `mapfile -t` (bash 3 lacks it on macOS).
  CRIT=()
  while IFS= read -r line; do
    case "$line" in
      ''|\#*) continue ;;
    esac
    CRIT+=("$line")
  done < "$PATHS_FILE"
}
mapfile_compat
if [ "${#CRIT[@]}" -eq 0 ]; then
  echo "::error::mode_b: $PATHS_FILE is empty" >&2
  exit 2
fi

# Resolve merge base. CI checkouts may be shallow; deepen on demand.
git fetch --no-tags --depth=50 origin main >/dev/null 2>&1 || true
if ! MB="$(git merge-base "$BASE" HEAD 2>/dev/null)"; then
  git fetch --unshallow origin main >/dev/null 2>&1 || true
  MB="$(git merge-base "$BASE" HEAD 2>/dev/null || true)"
fi
if [ -z "${MB:-}" ]; then
  echo "[!] mode_b: cannot resolve merge-base against $BASE; skipping"
  exit 0
fi

# Spec-concurrent edit short-circuit: if the PR also touches the spec
# body or the requirements manifest, or adds a new pytest req marker,
# orphan_check.py owns correctness. Pass.
SPEC_TOUCHED="$(git diff --name-only "$MB"...HEAD -- "$SPEC_BODY" "$SPEC_MANIFEST" || true)"
NEW_MARKERS="$(git diff "$MB"...HEAD -- 'tests/spec_conformance/**' \
  | grep -E '^\+[^+].*@pytest\.mark\.req\(' || true)"
if [ -n "$SPEC_TOUCHED" ] || [ -n "$NEW_MARKERS" ]; then
  echo "[+] mode_b: spec-concurrent edit detected; orphan_check owns this PR"
  exit 0
fi

# Substantive added-line count under critical paths.
# Filters out: deletions, blanks, comments/docstrings, imports,
# decorators, and bare type-hint annotations.
RAW_DIFF="$(git diff --find-renames=90% "$MB"...HEAD -- "${CRIT[@]}" || true)"
if [ -z "$RAW_DIFF" ]; then
  echo "[+] mode_b: no critical-path diff; OK"
  exit 0
fi

ADDED="$(printf '%s\n' "$RAW_DIFF" \
  | grep -E '^\+[^+]' \
  | grep -vE '^\+\s*$' \
  | grep -vE '^\+\s*(#|"""|'\'')' \
  | grep -vE '^\+\s*(from |import )' \
  | grep -vE '^\+\s*@' \
  | grep -vE '^\+\s*[a-zA-Z_][a-zA-Z0-9_]*\s*:\s*[A-Za-z_][A-Za-z0-9_\[\], |\.]*\s*(=.*)?$' \
  | wc -l | tr -d ' ')"

if [ "$ADDED" -lt "$THRESHOLD" ]; then
  echo "[+] mode_b: ${ADDED} substantive added lines (< ${THRESHOLD}); OK"
  exit 0
fi

# Waiver: PR body (via GH_PR_BODY env) OR a commit-message trailer.
WAIVER=""
if [ -n "${GH_PR_BODY:-}" ]; then
  WAIVER="$(printf '%s\n' "$GH_PR_BODY" | grep -E '^apm-spec-waiver:' | head -1 || true)"
fi
if [ -z "$WAIVER" ]; then
  WAIVER="$(git log --format=%B "$MB"..HEAD \
    | grep -E '^apm-spec-waiver:' | head -1 || true)"
fi
RATIONALE="${WAIVER#apm-spec-waiver:}"
RATIONALE="${RATIONALE# }"
if [ "${#RATIONALE}" -ge 16 ]; then
  echo "[!] mode_b: WAIVED (${ADDED} substantive added lines) -- ${RATIONALE}"
  exit 0
fi

# Fire.
echo "::error::Mode B detector: this PR adds ${ADDED} substantive lines under"
echo "OpenAPM critical paths without a corresponding spec citation."
echo ""
echo "Critical paths touched (added/removed by file):"
git diff --find-renames=90% --stat "$MB"...HEAD -- "${CRIT[@]}" | sed 's/^/  /'
echo ""
echo "OpenAPM v0.1 requires that net-new behaviour under normative"
echo "critical paths (manifest parser, lockfile writer, resolver,"
echo "policy engine, registry resolution, runtime, install,"
echo "integration) land WITH:"
echo "  1. a new <a id=\"req-XXX\"></a> anchor in the spec body,"
echo "  2. a matching row in the requirements manifest + Appendix C,"
echo "  3. a @pytest.mark.req(\"req-XXX\") test under tests/spec_conformance/."
echo ""
echo "See CONTRIBUTING.md \"Mode B (silent extension)\"."
echo ""
echo "To bypass intentionally (true refactor, perf rewrite, internal"
echo "cleanup with no observable behaviour delta), add this line to"
echo "the PR body or a commit message:"
echo ""
echo "  apm-spec-waiver: <one-line rationale, >= 16 chars>"
echo ""
echo "The waiver is echoed verbatim to the CI log."
exit 1
