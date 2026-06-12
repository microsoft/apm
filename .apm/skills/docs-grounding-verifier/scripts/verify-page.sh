#!/usr/bin/env bash
# End-to-end orchestrator for the grounding-verification pipeline.
#
# This script runs the DETERMINISTIC parts of the pipeline (extract-claims
# prompt-build, retrieve-evidence). LLM stages (claim extraction itself,
# grounding judgment) are dispatched by the CALLER -- this script emits
# the structured inputs the caller pipes to a model.
#
# USAGE:
#   verify-page.sh <claims_json> [--src-root <dir>] [--out <dir>]
#   verify-page.sh --help
#
# INPUT: a claims JSON file (output of stage 1 LLM call), shape:
#   {"page": "...", "claims": [{"id","text","keywords",...}, ...]}
#
# OUTPUT: writes one file per claim under <out>/evidence/<page>_<claim_id>.json
#         writes a single judge-prompts file <out>/judge-batch.txt that the
#         caller pipes to an LLM (one judgment per line is expected back).
#
# DIAGNOSTICS on stderr: claim count, evidence retrieval stats.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_ROOT="src/"
OUT_DIR="./grounding-out"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    --src-root) SRC_ROOT="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    *) CLAIMS_JSON="$1"; shift ;;
  esac
done

if [ -z "${CLAIMS_JSON:-}" ]; then
  echo "ERROR: claims JSON path required" >&2
  exit 2
fi
if [ ! -f "$CLAIMS_JSON" ]; then
  echo "ERROR: file not found: $CLAIMS_JSON" >&2
  exit 1
fi

mkdir -p "$OUT_DIR/evidence"
page=$(jq -r '.page' "$CLAIMS_JSON")
page_slug=$(echo "$page" | tr '/.' '__')
claim_count=$(jq '.claims | length' "$CLAIMS_JSON")

echo "[verify-page] page=$page claims=$claim_count" >&2

judge_batch="$OUT_DIR/judge-batch-${page_slug}.txt"
> "$judge_batch"

echo "JUDGE BATCH FOR PAGE: $page" >> "$judge_batch"
echo "Apply assets/judge-prompt.md to each tuple below." >> "$judge_batch"
echo "Return one JSON verdict per claim_id, in a JSON array." >> "$judge_batch"
echo "---" >> "$judge_batch"

jq -c '.claims[]' "$CLAIMS_JSON" | while IFS= read -r claim; do
  cid=$(echo "$claim" | jq -r '.id')
  evidence=$(echo "$claim" | bash "$SCRIPT_DIR/retrieve-evidence.sh" --root "$SRC_ROOT")
  echo "$evidence" > "$OUT_DIR/evidence/${page_slug}_${cid}.json"
  echo "" >> "$judge_batch"
  echo "TUPLE claim_id=$cid:" >> "$judge_batch"
  echo "$evidence" | jq '.' >> "$judge_batch"
done

echo "[verify-page] evidence written to $OUT_DIR/evidence/" >&2
echo "[verify-page] judge batch ready at $judge_batch" >&2
echo "$judge_batch"
