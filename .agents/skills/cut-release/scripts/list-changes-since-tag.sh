#!/usr/bin/env bash
# list-changes-since-tag.sh -- enumerate PRs merged since the last release tag.
#
# Emits a JSON array on stdout. Diagnostics on stderr. Non-interactive.
# Requires: git, jq. Uses gh (>= 2.40) when available.
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
# Enumeration: `gh pr list --state merged` is authoritative; a PR is kept
# only when its merge commit is reachable from HEAD and not from the last
# tag, so the window is exact. When gh is unavailable or errors, the
# script falls back to parsing first-parent `git log <tag>..HEAD`
# subjects for BOTH merge commits ("Merge pull request #N from ...") and
# squash titles ("... (#N)"), and warns on stderr.
#
# is_user_facing_guess is a HINT (true = touches user-visible surface;
# false = fully contained in maintainer/internal paths). The LLM caller
# in cut-release makes the final inclusion call per
# assets/entry-sanitizer.md.
#
# Exit codes:
#   0 success
#   1 missing dependency / missing last tag / git failure / self-test failure
#   2 usage error
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: list-changes-since-tag.sh [--help | --self-test]

Enumerate PRs merged since the last release tag (as determined by
`git describe --tags --abbrev=0`). Emits a JSON array on stdout.

PRs come from `gh pr list --state merged --base main`, kept only when
their merge commit is in <tag>..HEAD. Without gh (or if gh fails) the
script warns on stderr and parses `git log <tag>..HEAD` subjects for
both merge commits ("Merge pull request #N from ...") and squash
titles ("... (#N)").

Options:
  --help, -h    Show this help.
  --self-test   Build a throwaway git repo (tag + squash "(#11)" +
                "Merge pull request #12 from x/y") and assert the
                git-log parser finds both. Offline; does not use gh.

Environment:
  GH_TOKEN / GITHUB_TOKEN -- used by `gh` if set.

Examples:
  list-changes-since-tag.sh > /tmp/prs.json
  list-changes-since-tag.sh | jq '.[] | select(.is_user_facing_guess)'
  list-changes-since-tag.sh --self-test
EOF
}

# Print "NUM<TAB>SHA" (newest first, deduplicated) for every PR whose
# merge or squash commit is on the first-parent line of <range>.
parse_prs_from_git_log() {
    local range="$1" sha subject num
    git log "$range" --first-parent --pretty=format:'%H%x09%s%n' \
        | while IFS=$'\t' read -r sha subject; do
            [[ -z "$sha" ]] && continue
            num=""
            if [[ "$subject" =~ ^Merge\ pull\ request\ \#([0-9]+)\  ]]; then
                num="${BASH_REMATCH[1]}"
            elif [[ "$subject" =~ \(#([0-9]+)\)$ ]]; then
                num="${BASH_REMATCH[1]}"
            fi
            [[ -n "$num" ]] && printf '%s\t%s\n' "$num" "$sha"
        done | awk -F'\t' '!seen[$1]++'
}

# Print "NUM<TAB>SHA" (newest first) for merged PRs whose merge commit is
# reachable from HEAD and not from <tag>. Returns non-zero if gh fails.
list_prs_from_gh() {
    local tag="$1" since raw total oid num kept=0 skipped=0
    # Date-only bound in UTC: a superset of the window; the ancestry
    # filter below makes it exact.
    since=$(TZ=UTC git log -1 --format=%cd --date=short-local "$tag")
    raw=$(gh pr list --state merged --base main \
        --search "merged:>=${since}" --limit 500 \
        --json number,title,labels,author,mergeCommit,mergedAt) || return 1
    total=$(jq 'length' <<< "$raw") || return 1
    if [[ "$total" -ge 500 ]]; then
        echo "[!] gh returned $total PRs (limit 500); list may be truncated" >&2
    fi
    while IFS=$'\t' read -r num oid; do
        [[ -z "$num" ]] && continue
        if [[ -z "$oid" ]] || ! git cat-file -e "${oid}^{commit}" 2>/dev/null; then
            skipped=$((skipped + 1))
            continue
        fi
        if git merge-base --is-ancestor "$oid" HEAD \
            && ! git merge-base --is-ancestor "$oid" "$tag"; then
            printf '%s\t%s\n' "$num" "$oid"
            kept=$((kept + 1))
        fi
    done < <(jq -r 'sort_by(.mergedAt) | reverse | .[]
                    | "\(.number)\t\(.mergeCommit.oid // "")"' <<< "$raw")
    echo "[i] gh: $total merged PR(s) since $since; $kept in ${tag}..HEAD" \
        "($skipped merge commit(s) not in local history)" >&2
}

SELF_TEST_TMP=""
run_self_test() {
    local tmp found rc=0
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/list-changes-self-test.XXXXXX")
    SELF_TEST_TMP="$tmp"
    trap 'rm -rf "$SELF_TEST_TMP"' EXIT
    (
        cd "$tmp"
        export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
        g() { git -c user.name=t -c user.email=t@example.invalid \
                  -c commit.gpgsign=false -c tag.gpgsign=false "$@"; }
        g init -q -b main .
        g commit -q --allow-empty -m "chore: before tag (#10)"
        g tag v0.0.1
        g commit -q --allow-empty -m "fix: squash-merged change (#11)"
        g checkout -q -b feature
        g commit -q --allow-empty -m "feat: work on branch (#99)"
        g checkout -q main
        g merge -q --no-ff -m "Merge pull request #12 from x/y" feature
    ) >/dev/null
    found=$(cd "$tmp" && parse_prs_from_git_log "v0.0.1..HEAD" | cut -f1 | sort -n | tr '\n' ' ')
    for want in 11 12; do
        if [[ " $found" != *" $want "* ]]; then
            echo "[x] self-test: PR #$want not found (got: ${found:-none})" >&2
            rc=1
        fi
    done
    for unwanted in 10 99; do
        if [[ " $found" == *" $unwanted "* ]]; then
            echo "[x] self-test: PR #$unwanted wrongly included (got: $found)" >&2
            rc=1
        fi
    done
    if [[ $rc -eq 0 ]]; then
        echo "[+] self-test passed: found PRs ${found% }" >&2
    fi
    return $rc
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

if [[ "${1:-}" == "--self-test" && $# -eq 1 ]]; then
    if ! command -v git >/dev/null 2>&1; then
        echo "[x] missing required tool: git" >&2
        exit 1
    fi
    run_self_test || exit 1
    exit 0
fi

if [[ $# -gt 0 ]]; then
    echo "[x] unexpected arguments: $*" >&2
    usage >&2
    exit 2
fi

for tool in git jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[x] missing required tool: $tool" >&2
        exit 1
    fi
done
HAVE_GH=0
command -v gh >/dev/null 2>&1 && HAVE_GH=1

LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || true)
if [[ -z "$LAST_TAG" ]]; then
    echo "[x] no tags found; cannot enumerate range" >&2
    exit 1
fi
echo "[i] last release tag: $LAST_TAG" >&2

if [[ "$(git rev-parse --is-shallow-repository)" == "true" ]]; then
    echo "[!] shallow clone: deepen history past $LAST_TAG for an exact window" >&2
fi

if [[ -z "$(git rev-list -n 1 "${LAST_TAG}..HEAD")" ]]; then
    echo "[i] no commits since $LAST_TAG; emitting empty array" >&2
    echo "[]"
    exit 0
fi

PR_LINES=""
if [[ $HAVE_GH -eq 1 ]] && PR_LINES=$(list_prs_from_gh "$LAST_TAG"); then
    :
else
    if [[ $HAVE_GH -eq 1 ]]; then
        echo "[!] gh pr list failed; falling back to git log subject parsing" >&2
    else
        echo "[!] gh not found; falling back to git log subject parsing" >&2
    fi
    PR_LINES=$(parse_prs_from_git_log "${LAST_TAG}..HEAD")
fi

if [[ -z "$PR_LINES" ]]; then
    echo "[!] no merged PRs found since $LAST_TAG" >&2
    echo "[]"
    exit 0
fi

echo "[i] resolved $(grep -c . <<< "$PR_LINES") PR number(s)" >&2

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

# Offline PR details from the commit itself, used when `gh pr view` fails.
git_pr_json() {
    local num="$1" sha="$2" subject title author paths
    subject=$(git log -1 --format=%s "$sha")
    if [[ "$subject" =~ ^Merge\ pull\ request\ \# ]]; then
        title=$(git log -1 --format=%b "$sha" | sed -n '/./{p;q;}')
        [[ -z "$title" ]] && title="$subject"
        author=$(git log -1 --format=%an "${sha}^2" 2>/dev/null || true)
    else
        title="${subject% (#"$num")}"
        author=$(git log -1 --format=%an "$sha")
    fi
    paths=$(git diff --name-only "${sha}^1" "$sha" 2>/dev/null || true)
    jq -c -n --arg title "$title" --arg author "$author" --arg paths "$paths" \
        '{title: $title, labels: [], author: {login: $author},
          files: [$paths | split("\n")[] | select(length > 0) | {path: .}]}'
}

# Emit JSON array.
printf '['
FIRST=1
while IFS=$'\t' read -r NUM SHA; do
    [[ -z "$NUM" ]] && continue
    PR_JSON=""
    if [[ $HAVE_GH -eq 1 ]]; then
        PR_JSON=$(gh pr view "$NUM" --json title,labels,author,files 2>/dev/null || echo "")
    fi
    if [[ -z "$PR_JSON" ]]; then
        echo "[!] gh pr view failed for PR #$NUM; using git commit metadata" >&2
        PR_JSON=$(git_pr_json "$NUM" "$SHA")
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
    if [[ ${#PATHS_SUMMARY[@]} -eq 0 ]]; then
        PATHS_JSON="[]"
    else
        PATHS_JSON=$(printf '%s\n' "${PATHS_SUMMARY[@]}" | jq -R . | jq -s .)
    fi

    [[ $FIRST -eq 0 ]] && printf ','
    FIRST=0

    jq -c -n \
        --argjson pr "$NUM" \
        --arg title "$TITLE" \
        --argjson labels "$LABELS" \
        --arg author "$AUTHOR" \
        --argjson paths "$PATHS_JSON" \
        --arg sha "$SHA" \
        --argjson user_facing "$USER_FACING" \
        '{pr: $pr, title: $title, labels: $labels, author: $author,
          paths_summary: $paths, is_user_facing_guess: $user_facing,
          commit_sha: $sha}'
done <<< "$PR_LINES"
printf ']\n'
