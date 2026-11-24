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
for os in linux macos windows; do
  if [ "$os" = windows ]; then   # Windows tree-50 x 15 pairs is long: one request per fixture
    req $os tree-10 $INST 5; req $os tree-50 $INST 5
  else
    req $os tree-10,tree-50 $INST 5
  fi
  req $os tree-10,tree-50 $AUTH 2
  req $os tree-10,tree-50 $REST 2
  req $os tree-10-ghe,tree-50-ghe $ENT 2
done
