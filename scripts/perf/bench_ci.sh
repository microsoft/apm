#!/usr/bin/env bash
# Perf bench on CI runners: agents only do I/O (seconds), runners do the work.
#
#   scripts/perf/bench_ci.sh request BASE_SHA NEW_SHA [JOURNEYS] [FIXTURES] [PAIRS] [RTT] [OS]
#       -> pushes perf-bench/<new8>-<epoch> with bench-request.env; prints the branch.
#   scripts/perf/bench_ci.sh fetch BRANCH
#       -> waits for that branch's Perf bench run and prints its summary table.
#
# Defaults: the 4 North Star journeys on tree-10, 3 pairs per runner, RTT 30 ms,
# 3 runners (9 interleaved pairs per side), OS linux (or macos, windows).
# Wall time is never reported.
# The harness (workflow + tests/benchmarks) comes from HARNESS_REF.
set -euo pipefail

REPO="${PERF_BENCH_REPO:-microsoft/apm}"
HARNESS_REF="${PERF_BENCH_HARNESS_REF:-main}"
URL="https://github.com/${REPO}.git"

die() { echo "[x] $*" >&2; exit 1; }

request() {
  local base="${1:?BASE_SHA}" new="${2:?NEW_SHA}"
  local journeys="${3:-install-noop,install-locked-warm,install-warm,install-frozen-warm}"
  local fixtures="${4:-tree-10}" pairs="${5:-3}" rtt="${6:-30}" os="${7:-linux}"
  [[ "$base" =~ ^[0-9a-f]{7,40}$ && "$new" =~ ^[0-9a-f]{7,40}$ ]] || die "SHAs must be hex"
  local branch="perf-bench/${new:0:8}-$(date +%s)"
  local tmp; tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  git -C "$tmp" init --quiet
  git -C "$tmp" fetch --quiet --depth 1 "$URL" "$HARNESS_REF"
  git -C "$tmp" checkout --quiet FETCH_HEAD
  printf 'BASE=%s\nNEW=%s\nFIXTURES=%s\nJOURNEYS=%s\nRTT=%s\nPAIRS=%s\nOS=%s\n' \
    "$base" "$new" "$fixtures" "$journeys" "$rtt" "$pairs" "$os" > "$tmp/bench-request.env"
  git -C "$tmp" add bench-request.env
  git -C "$tmp" -c user.name="perf-bench" -c user.email="perf-bench@users.noreply.github.com" \
    commit --quiet -m "perf-bench: ${base:0:8} -> ${new:0:8}"
  git -C "$tmp" push --quiet "$URL" "HEAD:refs/heads/$branch" 2>/dev/null || die "push failed"
  echo "$branch"
}

fetch() {
  local branch="${1:?BRANCH}" run_id="" tries=0
  while [ -z "$run_id" ]; do
    run_id="$(gh run list --repo "$REPO" --workflow perf-bench.yml --branch "$branch" \
      --limit 1 --json databaseId --jq '.[0].databaseId // empty')"
    [ -n "$run_id" ] && break
    tries=$((tries + 1)); [ "$tries" -gt 30 ] && die "no Perf bench run for $branch"
    sleep 10
  done
  echo "[i] run https://github.com/$REPO/actions/runs/$run_id" >&2
  local status=0
  gh run watch "$run_id" --repo "$REPO" --interval 30 --exit-status >/dev/null 2>&1 || status=$?
  local out; out="$(mktemp -d)"
  gh run download "$run_id" --repo "$REPO" --name perf-bench-result --dir "$out" \
    || die "no result artifact (see the run log)"
  cat "$out/summary.md"
  echo "[i] json: $out/perf-bench-result.json" >&2
  git push --quiet "$URL" --delete "$branch" 2>/dev/null || true
  if [ "$status" -ne 0 ]; then
    echo "[x] the run failed: the table above is incomplete evidence" >&2
    return "$status"
  fi
}

case "${1:-}" in
  request) shift; request "$@" ;;
  fetch) shift; fetch "$@" ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
