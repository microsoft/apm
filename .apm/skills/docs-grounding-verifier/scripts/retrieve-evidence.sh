#!/usr/bin/env bash
# Stage 2 of grounding-verification pipeline.
# Deterministic evidence retrieval: given a claim JSON, runs ripgrep over
# src/ for each keyword and emits candidate source passages on stdout.
#
# NO LLM. Pure grep. This is the S7 DETERMINISTIC TOOL BRIDGE that
# prevents the judge from hallucinating evidence.
#
# USAGE:
#   echo '<claim_json>' | retrieve-evidence.sh [--root <dir>]
#   retrieve-evidence.sh --help
#
# OUTPUT (stdout, JSON one-line):
#   {"claim_id":..., "claim_text":..., "evidence":[{"file","line","snippet","matched_keyword"}], "evidence_count":N}
# DIAGNOSTICS (stderr): retrieval stats.

set -euo pipefail

ROOT="src/"
CONTEXT_LINES=2
MAX_HITS_PER_KEYWORD=5

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    --root) ROOT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq required" >&2; exit 2; }
if command -v rg >/dev/null 2>&1; then
  SEARCH_CMD="rg --no-heading --line-number --max-count=$MAX_HITS_PER_KEYWORD -F"
else
  # Portable fallback: GNU/BSD grep -rn with fixed-strings.
  SEARCH_CMD="grep -rn --include=*.py --include=*.md -F"
fi

claim_json=$(cat)

claim_id=$(echo "$claim_json" | jq -r '.id // "unknown"')
claim_text=$(echo "$claim_json" | jq -r '.text // ""')
keywords=$(echo "$claim_json" | jq -r '.keywords[]?' 2>/dev/null || true)
hints=$(echo "$claim_json" | jq -r '.expected_source_areas[]?' 2>/dev/null || true)

search_paths=("$ROOT")
hint_paths=()
if [ -n "$hints" ]; then
  while IFS= read -r h; do
    [ -z "$h" ] && continue
    [ -e "$h" ] && hint_paths+=("$h")
  done <<< "$hints"
fi
if [ ${#hint_paths[@]} -gt 0 ]; then
  search_paths=("${hint_paths[@]}" "$ROOT")
fi

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
echo '[]' > "$tmp"

while IFS= read -r kw; do
  [ -z "$kw" ] && continue
  hits=$($SEARCH_CMD "$kw" "${search_paths[@]}" 2>/dev/null | head -n "$MAX_HITS_PER_KEYWORD" || true)
  [ -z "$hits" ] && continue
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    file=$(echo "$line" | awk -F: '{print $1}')
    lineno=$(echo "$line" | awk -F: '{print $2}')
    snippet=$(echo "$line" | cut -d: -f3- | cut -c1-200)
    new_item=$(jq -nc --arg f "$file" --arg l "$lineno" --arg s "$snippet" --arg k "$kw" \
      '{file:$f, line:($l|tonumber? // 0), snippet:$s, matched_keyword:$k}')
    jq --argjson item "$new_item" '. + [$item]' "$tmp" > "$tmp.new" && mv "$tmp.new" "$tmp"
  done <<< "$hits"
done <<< "$keywords"

ev_count=$(jq 'length' "$tmp")
jq -c --arg cid "$claim_id" --arg ctext "$claim_text" \
  '{claim_id:$cid, claim_text:$ctext, evidence:., evidence_count:length}' "$tmp"

echo "[retrieve-evidence] claim=$claim_id evidence_count=$ev_count" >&2
