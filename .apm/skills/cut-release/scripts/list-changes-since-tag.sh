#!/usr/bin/env bash
# list-changes-since-tag.sh -- enumerate PRs merged since the last release tag.
#
# Emits a JSON array on stdout. Diagnostics on stderr. Non-interactive.
# Requires: git, gh (>= 2.40), jq.
#
# Output schema (per PR):
#   {
#     "pr": 1495,
#     "title": "feat(deps): add 'apm deps why <pkg>' ...",
#     "labels": ["enhancement"],
#     "author": "danielmeppiel",
#     "paths_summary": ["src/apm_cli/...", "tests/...", "docs/..."],
#     "is_user_facing_guess": true,
#     "commit_sha": "61fe5066..."
#   }
#
# is_user_facing_guess is a HINT (true = touches user-visible surface;
# false = fully contained in maintainer/internal paths). The LLM caller
# in cut-release makes the final inclusion call per
# assets/entry-sanitizer.md.
#
# Exit codes:
#   0 success
#   1 missing dependency / missing last tag / git failure
#   2 usage error
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: list-changes-since-tag.sh [--help]

Enumerate PRs merged since the last release tag (as determined by
`git describe --tags --abbrev=0`). Emits a JSON array on stdout.

No arguments. Stops at the first git/gh failure.

Environment:
  GH_TOKEN / GITHUB_TOKEN -- used by `gh` if set.

Examples:
  list-changes-since-tag.sh > /tmp/prs.json
  list-changes-since-tag.sh | jq '.[] | select(.is_user_facing_guess)'
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

for tool in git gh jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[x] missing required tool: $tool" >&2
        exit 1
    fi
done

LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || true)
if [[ -z "$LAST_TAG" ]]; then
    echo "[x] no tags found; cannot enumerate range" >&2
    exit 1
fi
echo "[i] last release tag: $LAST_TAG" >&2

# Collect (sha, subject) for non-merge commits since the tag.
COMMITS=$(git log "${LAST_TAG}..HEAD" --no-merges --pretty=format:'%H%x09%s')
if [[ -z "$COMMITS" ]]; then
    echo "[i] no commits since $LAST_TAG; emitting empty array" >&2
    echo "[]"
    exit 0
fi

# Extract PR numbers from commit subjects (squash-merge convention: "(#NNNN)").
# Fallback: use `gh pr list --search` keyed by sha if no number is in the subject.
PR_NUMS=()
declare -A SHA_OF_PR
while IFS=$'\t' read -r SHA SUBJECT; do
    if [[ "$SUBJECT" =~ \(#([0-9]+)\)$ ]]; then
        NUM="${BASH_REMATCH[1]}"
        PR_NUMS+=("$NUM")
        SHA_OF_PR[$NUM]="$SHA"
    fi
done <<< "$COMMITS"

if [[ ${#PR_NUMS[@]} -eq 0 ]]; then
    echo "[!] no PR numbers extracted from commit subjects" >&2
    echo "[]"
    exit 0
fi

echo "[i] resolved ${#PR_NUMS[@]} PR number(s)" >&2

# Internal-path predicate. A path is "internal" if it falls entirely
# inside any of these prefixes.
internal_path() {
    local p="$1"
    case "$p" in
        .apm/*|.github/instructions/*|.github/workflows/*|.github/ISSUE_TEMPLATE/*|tests/*|docs/*)
            return 0 ;;
        *)
            return 1 ;;
    esac
}

# Emit JSON array.
printf '['
FIRST=1
for NUM in "${PR_NUMS[@]}"; do
    PR_JSON=$(gh pr view "$NUM" --json title,labels,author,files 2>/dev/null || echo "")
    if [[ -z "$PR_JSON" ]]; then
        echo "[!] failed to fetch PR #$NUM; skipping" >&2
        continue
    fi

    TITLE=$(jq -r '.title' <<< "$PR_JSON")
    LABELS=$(jq -c '[.labels[].name]' <<< "$PR_JSON")
    AUTHOR=$(jq -r '.author.login' <<< "$PR_JSON")
    PATHS=$(jq -r '.files[].path' <<< "$PR_JSON")

    # Classify: user-facing iff at least one path is NOT internal.
    USER_FACING="false"
    PATHS_SUMMARY=()
    while IFS= read -r P; do
        [[ -z "$P" ]] && continue
        PATHS_SUMMARY+=("$P")
        if ! internal_path "$P"; then
            USER_FACING="true"
        fi
    done <<< "$PATHS"

    # Cap paths_summary at 10 entries (diagnostic, not exhaustive).
    if [[ ${#PATHS_SUMMARY[@]} -gt 10 ]]; then
        PATHS_SUMMARY=("${PATHS_SUMMARY[@]:0:10}")
        PATHS_SUMMARY+=("...")
    fi
    PATHS_JSON=$(printf '%s\n' "${PATHS_SUMMARY[@]}" | jq -R . | jq -s .)

    [[ $FIRST -eq 0 ]] && printf ','
    FIRST=0

    jq -c -n \
        --argjson pr "$NUM" \
        --arg title "$TITLE" \
        --argjson labels "$LABELS" \
        --arg author "$AUTHOR" \
        --argjson paths "$PATHS_JSON" \
        --arg sha "${SHA_OF_PR[$NUM]}" \
        --argjson user_facing "$USER_FACING" \
        '{pr: $pr, title: $title, labels: $labels, author: $author,
          paths_summary: $paths, is_user_facing_guess: $user_facing,
          commit_sha: $sha}'
done
printf ']\n'
