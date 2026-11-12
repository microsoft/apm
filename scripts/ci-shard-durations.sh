#!/usr/bin/env bash
# Choose the test-duration files that CI shard planners balance with.
#
# Prefers the newest successful nightly build-release.yml run's
# integration-durations artifact (measured on the current corpus) and falls
# back to the committed seed in .github/ci-durations/. Shards then read the
# exact files the planner used, so every shard computes the same partition.
#
# Usage: scripts/ci-shard-durations.sh <out-dir>
set -euo pipefail

OUT="${1:?usage: $0 <out-dir>}"
SEED_DIR=".github/ci-durations"
ARTIFACT="integration-durations"
mkdir -p "$OUT"
cp "$SEED_DIR"/*.json "$OUT"/

run_id="$(timeout 60 gh run list --repo "$GITHUB_REPOSITORY" --workflow build-release.yml \
  --event schedule --status success --limit 1 --json databaseId --jq '.[0].databaseId' \
  2>/dev/null || true)"
if [ -n "$run_id" ] && [ "$run_id" != "null" ]; then
  nightly="$OUT/.nightly"
  if timeout 120 gh run download "$run_id" --repo "$GITHUB_REPOSITORY" \
    --name "$ARTIFACT" --dir "$nightly" 2>/dev/null; then
    cp "$nightly"/*.json "$OUT"/ && rm -rf "$nightly"
    echo "[i] Shard durations: nightly run $run_id ($ARTIFACT) over the committed seed"
    exit 0
  fi
fi
echo "[i] Shard durations: committed seed in $SEED_DIR (no nightly $ARTIFACT artifact)"
