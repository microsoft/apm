#!/usr/bin/env bash
# Dispatch the CEO-report bench on CI: BASE e37400fa (main before the epic) vs NEW (fat-train head or main).
# Usage: dispatch_ci.sh <NEW 40-char sha>. Appends request lines to data/requests.log for collect_ci.py.
set -uo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
NEW="${1:?NEW sha (40 hex)}"; [[ "$NEW" =~ ^[0-9a-f]{40}$ ]] || { echo "NEW must be 40 hex"; exit 2; }
BASE=e37400fa313bfe736ec63242299486dac7647961
export PERF_BENCH_HARNESS_REF=danielmeppiel-perf-ceo-report
mkdir -p "$D/data"; LOG="$D/data/requests.log"
echo "NEW=$NEW BASE=$BASE harness=$PERF_BENCH_HARNESS_REF $(date '+%H:%M %Z')" >> "$LOG"
INST=install-cold,install-warm,install-noop,install-noop-replay,install-frozen   # PAIRS=5 -> 15 pairs
AUTH=install-cold-auth,install-warm-auth
REST=update,uninstall,compile,audit,audit-ci,pack,install-bundle,outdated,prune,boot-version,boot-help
ENT=install-cold,install-warm,install-noop,install-cold-auth
req() { # os fixtures journeys pairs
  for i in 1 2 3 4; do
    b=$(bash "$D/bench_ci.sh" request "$BASE" "$NEW" "$3" "$2" "$4" 30 "$1" 0 2>/dev/null) \
      && { echo "${BASE:0:8} $1 $2 $3 $b" >> "$LOG"; echo "ok $1 $2 pairs=$4 $3 -> $b"; return; }
    sleep 15
  done; echo "FAILED $1 $2 $3"; }
# install-frozen-noop (E2, closes #4770 if wall p50 <= 1 s at tree-10): Linux only, if the harness has it.
# FROZEN_NOOP=1/0 forces it on/off; default = detect in the harness journeys.py.
if [ -z "${FROZEN_NOOP:-}" ]; then
  gh api "repos/microsoft/apm/contents/tests/benchmarks/journeys.py?ref=$PERF_BENCH_HARNESS_REF" -q .content 2>/dev/null \
    | base64 -d 2>/dev/null | grep -c '"install-frozen-noop"' >/dev/null && FROZEN_NOOP=1 || FROZEN_NOOP=0
fi
echo "install-frozen-noop in Linux request: $FROZEN_NOOP"
# Priority first (PERF VP 05:32: #4939 cold t50 ratios + frozen-noop t10 by 06:15): small cold-only requests.
for os in linux macos windows; do
  if [ "$os" = linux ] && [ "$FROZEN_NOOP" = 1 ]; then req $os tree-10,tree-50 install-cold,install-frozen-noop 5
  elif [ "$os" = windows ]; then req $os tree-50 install-cold 5; req $os tree-10 install-cold 5
  else req $os tree-10,tree-50 install-cold 5; fi
done
REST_INST=install-warm,install-noop,install-noop-replay,install-frozen
for os in linux macos windows; do
  if [ "$os" = windows ]; then req $os tree-10 $REST_INST 5; req $os tree-50 $REST_INST 5
  else req $os tree-10,tree-50 $REST_INST 5; fi
  req $os tree-10,tree-50 $AUTH 2
  req $os tree-10,tree-50 $REST 2
  req $os tree-10-ghe,tree-50-ghe $ENT 2
done
