#!/usr/bin/env bash
# merge_gate_wait.sh -- poll the GitHub Checks API for an expected required
# check on a given SHA and emit a single pass/fail verdict. Used by
# .github/workflows/merge-gate.yml as the orchestrator's core logic.
#
# Why this script exists:
#   GitHub's required-status-checks model is name-based, not workflow-based.
#   When the underlying workflow fails to dispatch (transient webhook
#   delivery failure on `pull_request`), the required check stays in
#   "Expected -- Waiting" forever and the PR is silently stuck. This script
#   turns that ambiguous yellow into an unambiguous red after a bounded
#   liveness window, so reviewers see a real failure with a real message.
#
# Inputs (environment variables):
#   GH_TOKEN          required. Token with `checks:read` for the repo.
#   REPO              required. owner/repo (e.g. microsoft/apm).
#   SHA               required. Head SHA of the PR.
#   EXPECTED_CHECK    optional. Check-run name to wait for.
#                     Default: "Build & Test (Linux)".
#   TIMEOUT_MIN       optional. Total wall-clock budget in minutes.
#                     Default: 30.
#   POLL_SEC          optional. Poll interval in seconds. Default: 30.
#
# Exit codes:
#   0  expected check completed with conclusion success | skipped | neutral
#   1  expected check completed with a failing conclusion
#   2  expected check never appeared within TIMEOUT_MIN (THE BUG we catch)
#   3  expected check appeared but did not complete within TIMEOUT_MIN
#   4  invalid arguments / environment

set -euo pipefail

EXPECTED_CHECK="${EXPECTED_CHECK:-Build & Test (Linux)}"
TIMEOUT_MIN="${TIMEOUT_MIN:-30}"
POLL_SEC="${POLL_SEC:-30}"

if [ -z "${GH_TOKEN:-}" ] || [ -z "${REPO:-}" ] || [ -z "${SHA:-}" ]; then
  echo "ERROR: GH_TOKEN, REPO, and SHA are required." >&2
  exit 4
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI is required." >&2
  exit 4
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required." >&2
  exit 4
fi

deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
poll_count=0
ever_seen="false"

echo "[merge-gate] waiting for check '${EXPECTED_CHECK}' on ${REPO}@${SHA}"
echo "[merge-gate] timeout=${TIMEOUT_MIN}m poll=${POLL_SEC}s"

while [ "$(date +%s)" -lt "$deadline" ]; do
  poll_count=$((poll_count + 1))

  # Filter by check-run name server-side. Most-recent check-run is first.
  payload=$(gh api \
    -H "Accept: application/vnd.github+json" \
    "repos/${REPO}/commits/${SHA}/check-runs?check_name=$(jq -rn --arg n "$EXPECTED_CHECK" '$n|@uri')&per_page=10" \
    2>/dev/null) || payload='{"check_runs":[]}'

  total=$(echo "$payload" | jq '.check_runs | length' 2>/dev/null || echo 0)
  case "$total" in
    ''|*[!0-9]*) total=0 ;;
  esac

  if [ "$total" -gt 0 ]; then
    ever_seen="true"
    # Take the most recently started run for this name.
    status=$(echo "$payload" | jq -r '.check_runs | sort_by(.started_at) | reverse | .[0].status')
    conclusion=$(echo "$payload" | jq -r '.check_runs | sort_by(.started_at) | reverse | .[0].conclusion')
    url=$(echo "$payload" | jq -r '.check_runs | sort_by(.started_at) | reverse | .[0].html_url')

    echo "[merge-gate] poll #${poll_count}: status=${status} conclusion=${conclusion}"

    if [ "$status" = "completed" ]; then
      echo "[merge-gate] tier 1 finished: ${conclusion}"
      echo "[merge-gate] details: ${url}"
      case "$conclusion" in
        success|skipped|neutral)
          exit 0
          ;;
        *)
          echo "::error title=Tier 1 failed::'${EXPECTED_CHECK}' reported '${conclusion}'. See ${url}"
          exit 1
          ;;
      esac
    fi
  else
    echo "[merge-gate] poll #${poll_count}: '${EXPECTED_CHECK}' not yet present"
  fi

  sleep "$POLL_SEC"
done

if [ "$ever_seen" = "false" ]; then
  cat <<EOF >&2
::error title=Tier 1 never started::The required check '${EXPECTED_CHECK}' did not appear for SHA ${SHA} within ${TIMEOUT_MIN} minutes.

This usually indicates a transient GitHub Actions webhook delivery failure for the 'pull_request' event. Recovery:
  1. Push an empty commit to retrigger:  git commit --allow-empty -m 'ci: retrigger' && git push
  2. If that fails, close and reopen the PR.

This gate (Merge Gate) catches the failure mode so it surfaces as a clear red check instead of a stuck 'Expected -- Waiting'. See .github/workflows/merge-gate.yml.
EOF
  exit 2
fi

echo "::error title=Tier 1 timeout::Build & Test (Linux) appeared but did not complete within ${TIMEOUT_MIN} minutes." >&2
exit 3
