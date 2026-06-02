#!/usr/bin/env bash
# bump-version.sh -- bump pyproject.toml version + refresh uv.lock.
#
# Edits the `version = "..."` line under [project] in pyproject.toml,
# then runs `uv lock` to update uv.lock. Emits a unified diff of both
# files on stdout for review.
#
# Exit codes:
#   0 success
#   1 malformed input / target equals current version / file missing
#   2 usage error
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bump-version.sh <new_version>

Edit `version = "..."` in pyproject.toml to <new_version>, then
refresh uv.lock via `uv lock`. Emit a unified diff of both files
on stdout.

<new_version> must match the pattern MAJOR.MINOR.PATCH (digits
only, dot-separated, no v-prefix, no pre-release suffix).

Examples:
  bump-version.sh 0.16.0
  bump-version.sh 0.16.1
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

if [[ $# -ne 1 ]]; then
    echo "[x] expected exactly one argument: <new_version>" >&2
    usage >&2
    exit 2
fi

NEW_VERSION="$1"

if ! [[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "[x] malformed version: $NEW_VERSION (expected MAJOR.MINOR.PATCH)" >&2
    exit 1
fi

if [[ ! -f pyproject.toml ]]; then
    echo "[x] pyproject.toml not found in cwd: $(pwd)" >&2
    exit 1
fi

CURRENT=$(grep -E '^version = ' pyproject.toml | head -1 | sed -E 's/^version = "([^"]+)".*/\1/')
if [[ -z "$CURRENT" ]]; then
    echo "[x] could not parse current version from pyproject.toml" >&2
    exit 1
fi

if [[ "$CURRENT" == "$NEW_VERSION" ]]; then
    echo "[x] target version equals current ($CURRENT); nothing to do" >&2
    exit 1
fi

echo "[i] current: $CURRENT" >&2
echo "[i] target:  $NEW_VERSION" >&2

if ! command -v uv >/dev/null 2>&1; then
    echo "[x] missing required tool: uv" >&2
    exit 1
fi

# Snapshot for diff.
BEFORE_PYPROJECT=$(mktemp)
BEFORE_LOCK=$(mktemp)
trap 'rm -f "$BEFORE_PYPROJECT" "$BEFORE_LOCK"' EXIT
cp pyproject.toml "$BEFORE_PYPROJECT"
if [[ -f uv.lock ]]; then
    cp uv.lock "$BEFORE_LOCK"
fi

# Edit pyproject.toml in place. Match only the top-level project
# version (the first match anchored at column 0).
TMP=$(mktemp)
awk -v cur="$CURRENT" -v new="$NEW_VERSION" '
    !done && $0 == "version = \"" cur "\"" { print "version = \"" new "\""; done=1; next }
    { print }
' pyproject.toml > "$TMP"

if ! grep -q "^version = \"$NEW_VERSION\"$" "$TMP"; then
    echo "[x] failed to apply version edit (no matching line found)" >&2
    rm -f "$TMP"
    exit 1
fi
mv "$TMP" pyproject.toml

# Refresh uv.lock.
echo "[*] running uv lock" >&2
if ! uv lock >&2; then
    echo "[x] uv lock failed" >&2
    exit 1
fi

# Emit unified diff on stdout.
echo "=== pyproject.toml ==="
diff -u "$BEFORE_PYPROJECT" pyproject.toml || true
echo
echo "=== uv.lock (apm-cli package entry) ==="
if [[ -f "$BEFORE_LOCK" ]]; then
    diff -u "$BEFORE_LOCK" uv.lock | grep -E '^[-+@].*(apm-cli|version)' | head -20 || true
else
    echo "(uv.lock did not exist before)"
fi

echo
echo "[+] bumped $CURRENT -> $NEW_VERSION"
