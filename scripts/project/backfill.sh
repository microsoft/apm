#!/usr/bin/env bash
# Backfill all open issues+PRs in microsoft/apm that carry any theme/* label
# into the PGS project board, then sync their fields.
#
# Requires: GITHUB_TOKEN env var with project + repo scopes.
# Usage:    scripts/project/backfill.sh [--limit N]
set -euo pipefail

REPO="microsoft/apm"
LIMIT="${LIMIT:-100}"
PROJECT_ID="PVT_kwDOAF3p4s4BVoGw"
HERE="$(cd "$(dirname "$0")" && pwd)"

export PAGER=cat GH_PAGER=cat

echo "Fetching open issues + PRs with any theme/* label..."
ISSUES=""
# NOTE: keep this theme list in sync with THEME_MAP in sync_item.py.
# Search query OR semantics require one round-trip per theme; results are
# unioned via `sort -u` below.
for THEME in theme/portability theme/security theme/governance; do
  CHUNK=$(gh api graphql -f query='
  {
    search(query: "repo:microsoft/apm is:open label:\"'$THEME'\"", type: ISSUE, first: '$LIMIT') {
      nodes { ... on Issue { id number title } ... on PullRequest { id number title } }
    }
  }' --jq '.data.search.nodes[] | "\(.id)\t#\(.number) \(.title)"')
  ISSUES=$(printf "%s\n%s" "$ISSUES" "$CHUNK")
done
ISSUES=$(echo "$ISSUES" | grep -v '^$' | sort -u)

echo "$ISSUES" | while IFS=$'\t' read -r ID REF; do
  if [ -z "$ID" ]; then continue; fi
  echo "==> $REF"
  python3 "$HERE/sync_item.py" --content-id "$ID" --project-id "$PROJECT_ID" || echo "  FAIL on $REF"
done

echo "Done."
