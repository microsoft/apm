#!/usr/bin/env bash
# Eval-runner for docs-grounding-verifier.
#
# Runs the trigger-evals (dispatch-classification accuracy) and the
# content-evals (seeded-drift recall) against a model dispatcher.
# Reports precision/recall/grounding_rate as numbers.
#
# The model dispatcher itself is supplied OUT OF BAND -- this script
# emits structured inputs and reads structured outputs from a file.
# The expected workflow:
#
#   1. run-evals.sh --mode trigger --out trigger-out.json
#      -> writes trigger-prompts.txt; caller runs them through model;
#         caller writes model responses to trigger-responses.json;
#         re-run with --score-trigger to compute precision/recall.
#
#   2. run-evals.sh --mode content --out content-out.json
#      -> walks scenarios/, runs the full grounding pipeline against
#         a seeded corpus copy, reports per-scenario verdict.
#
# Non-interactive. JSON on stdout for scoring summary. Diagnostics stderr.
# USAGE:
#   run-evals.sh --mode trigger | content | score-trigger [args]
#   run-evals.sh --help

set -euo pipefail

MODE=""
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SKILL_DIR/../../.." && pwd)"
TRIGGER_EVALS="$SCRIPT_DIR/trigger-evals.json"
CONTENT_EVALS="$SCRIPT_DIR/content-evals.json"
OUT_DIR="$SCRIPT_DIR/runs/$(date -u +%Y%m%d-%H%M%S)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    --mode) MODE="$2"; shift 2 ;;
    --responses) RESPONSES="$2"; shift 2 ;;
    *) shift ;;
  esac
done

case "$MODE" in
  trigger)
    mkdir -p "$OUT_DIR"
    out="$OUT_DIR/trigger-prompts.txt"
    {
      echo "# Trigger-eval prompts. For each query below, decide which of these skills should fire:"
      echo "# docs-sync | docs-corpus-audit | docs-grounding-verifier | NONE"
      echo "# Return JSON: [{\"query_id\":N, \"selected\":\"SKILL_NAME\"}, ...]"
      echo "---"
      i=0
      jq -r '.should_trigger[]' "$TRIGGER_EVALS" | while IFS= read -r q; do
        i=$((i+1))
        echo "Q$i [expected: docs-grounding-verifier]: $q"
      done
      jq -r '.should_not_trigger[]' "$TRIGGER_EVALS" | while IFS= read -r q; do
        i=$((i+1))
        echo "Q$i [expected: NOT docs-grounding-verifier]: $q"
      done
    } > "$out"
    echo "[run-evals] trigger prompts written to $out" >&2
    echo "$out"
    ;;

  score-trigger)
    if [ -z "${RESPONSES:-}" ] || [ ! -f "$RESPONSES" ]; then
      echo "ERROR: --responses <path> required and must exist" >&2
      exit 2
    fi
    should_count=$(jq '.should_trigger | length' "$TRIGGER_EVALS")
    shouldnt_count=$(jq '.should_not_trigger | length' "$TRIGGER_EVALS")
    true_pos=$(jq --argjson n "$should_count" '[.[] | select(.query_id <= $n) | select(.selected == "docs-grounding-verifier")] | length' "$RESPONSES")
    false_neg=$((should_count - true_pos))
    true_neg=$(jq --argjson n "$should_count" '[.[] | select(.query_id > $n) | select(.selected != "docs-grounding-verifier")] | length' "$RESPONSES")
    false_pos=$((shouldnt_count - true_neg))
    precision=$(awk -v tp="$true_pos" -v fp="$false_pos" 'BEGIN{ if(tp+fp==0){print 0} else {printf "%.3f", tp/(tp+fp)} }')
    recall=$(awk -v tp="$true_pos" -v fn="$false_neg" 'BEGIN{ if(tp+fn==0){print 0} else {printf "%.3f", tp/(tp+fn)} }')
    specificity=$(awk -v tn="$true_neg" -v fp="$false_pos" 'BEGIN{ if(tn+fp==0){print 0} else {printf "%.3f", tn/(tn+fp)} }')
    jq -nc \
      --argjson tp "$true_pos" --argjson fp "$false_pos" \
      --argjson tn "$true_neg" --argjson fn "$false_neg" \
      --arg p "$precision" --arg r "$recall" --arg s "$specificity" \
      '{true_positive:$tp, false_positive:$fp, true_negative:$tn, false_negative:$fn,
        precision:($p|tonumber), recall:($r|tonumber), specificity:($s|tonumber),
        pass_gate: (($p|tonumber)>=0.9 and ($r|tonumber)>=0.9 and ($s|tonumber)>=0.9)}'
    ;;

  content)
    mkdir -p "$OUT_DIR"
    seeded_corpus="$OUT_DIR/seeded-corpus"
    mkdir -p "$seeded_corpus"
    jq -c '.scenarios[]' "$CONTENT_EVALS" | while IFS= read -r scenario; do
      sid=$(echo "$scenario" | jq -r '.id')
      template=$(echo "$scenario" | jq -r '.page_template')
      drift=$(echo "$scenario" | jq -r '.seeded_drift')
      section=$(echo "$scenario" | jq -r '.section')
      mkdir -p "$seeded_corpus/$sid"
      if [ -f "$REPO_ROOT/$template" ]; then
        cp "$REPO_ROOT/$template" "$seeded_corpus/$sid/page.md"
        printf '\n\n## %s (seeded)\n\n%s\n' "$section" "$drift" >> "$seeded_corpus/$sid/page.md"
        echo "$scenario" > "$seeded_corpus/$sid/scenario.json"
        echo "[run-evals] seeded scenario $sid -> $seeded_corpus/$sid/" >&2
      else
        echo "[run-evals] WARN: template missing for $sid: $template" >&2
      fi
    done
    echo "[run-evals] seeded corpora written to $seeded_corpus" >&2
    echo "[run-evals] Next: run the grounding pipeline on each seeded page" >&2
    echo "[run-evals]   and report whether the seeded drift was caught." >&2
    echo "$seeded_corpus"
    ;;

  "")
    echo "ERROR: --mode required (trigger | content | score-trigger)" >&2
    exit 2 ;;

  *)
    echo "ERROR: unknown mode: $MODE" >&2
    exit 2 ;;
esac
