#!/usr/bin/env bash
# merge_gate_wait.sh -- poll the GitHub Checks API for a list of expected
# required checks on a given SHA and emit a single pass/fail verdict. Used
# by .github/workflows/merge-gate.yml as the orchestrator's core logic.
#
# Why this script exists:
#   GitHub's required-status-checks model is name-based, not workflow-based.
#   When the underlying workflow fails to dispatch (transient webhook
#   delivery failure on 'pull_request'), the required check stays in
#   "Expected -- Waiting" forever and the PR is silently stuck. This script
#   turns that ambiguous yellow into an unambiguous red after a bounded
#   liveness window, so reviewers see a real failure with a real message.
#
#   It also lets us collapse N separately-required checks into a single
#   required gate (Tide / bors pattern). Branch protection only requires
#   "Merge Gate / gate"; this script verifies all underlying checks.
#
# Inputs (environment variables):
#   GH_TOKEN          required. Token with 'checks:read' for the repo.
#   REPO              required. owner/repo (e.g. microsoft/apm).
#   SHA               required. Head SHA of the PR.
#   EXPECTED_CHECKS   required. Comma-separated list of check-run names to
#                     wait for. Whitespace around commas is trimmed.
#                     Example: "Build & Test (Linux),Build (Linux)"
#   TIMEOUT_MIN       optional. Total wall-clock budget in minutes.
#                     Default: 30.
#   POLL_SEC          optional. Poll interval in seconds. Default: 30.
#
# Exit codes:
#   0  all expected checks completed with success | skipped | neutral
#   1  at least one expected check completed with a failing conclusion
#   2  at least one expected check never appeared within TIMEOUT_MIN
#      (THE BUG we catch -- dropped 'pull_request' webhook)
#   3  at least one expected check appeared but did not complete in time
#   4  invalid arguments / environment

set -euo pipefail

EXPECTED_CHECKS="${EXPECTED_CHECKS:-}"
TIMEOUT_MIN="${TIMEOUT_MIN:-30}"
POLL_SEC="${POLL_SEC:-30}"

if [ -z "${GH_TOKEN:-}" ] || [ -z "${REPO:-}" ] || [ -z "${SHA:-}" ] || [ -z "$EXPECTED_CHECKS" ]; then
  echo "ERROR: GH_TOKEN, REPO, SHA, and EXPECTED_CHECKS are required." >&2
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

# Parse EXPECTED_CHECKS into an array (split on comma, trim whitespace).
declare -a checks=()
IFS=',' read -ra raw <<< "$EXPECTED_CHECKS"
for c in "${raw[@]}"; do
  trimmed="$(echo "$c" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -n "$trimmed" ] && checks+=("$trimmed")
done

if [ "${#checks[@]}" -eq 0 ]; then
  echo "ERROR: EXPECTED_CHECKS parsed to an empty list." >&2
  exit 4
fi

# Per-check state held in two parallel indexed arrays (avoids bash 4+
# associative arrays so the script also works on stock macOS bash 3.2).
# Status values: pending, ok, fail, missing
declare -a check_status=()
declare -a check_url=()
for _ in "${checks[@]}"; do
  check_status+=("pending")
  check_url+=("")
done

deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
poll_count=0

echo "[merge-gate] waiting for ${#checks[@]} check(s) on ${REPO}@${SHA}"
for c in "${checks[@]}"; do
  echo "[merge-gate]   - ${c}"
done
echo "[merge-gate] timeout=${TIMEOUT_MIN}m poll=${POLL_SEC}s"

while [ "$(date +%s)" -lt "$deadline" ]; do
  poll_count=$((poll_count + 1))
  pending_count=0

  for i in "${!checks[@]}"; do
    c="${checks[i]}"
    [ "${check_status[i]}" = "pending" ] || continue
    pending_count=$((pending_count + 1))

    # Filter by check-run name server-side. Most-recent first.
    encoded=$(jq -rn --arg n "$c" '$n|@uri')
    payload=$(gh api \
      -H "Accept: application/vnd.github+json" \
      "repos/${REPO}/commits/${SHA}/check-runs?check_name=${encoded}&per_page=10" \
      2>/dev/null) || payload='{"check_runs":[]}'

    total=$(echo "$payload" | jq '.check_runs | length' 2>/dev/null || echo 0)
    case "$total" in ''|*[!0-9]*) total=0 ;; esac

    if [ "$total" -eq 0 ]; then
      echo "[merge-gate] poll #${poll_count}: '${c}' not yet present"
      continue
    fi

    status=$(echo "$payload" | jq -r '.check_runs | sort_by(.started_at) | reverse | .[0].status')
    conclusion=$(echo "$payload" | jq -r '.check_runs | sort_by(.started_at) | reverse | .[0].conclusion')
    url=$(echo "$payload" | jq -r '.check_runs | sort_by(.started_at) | reverse | .[0].html_url')
    check_url[i]="$url"

    if [ "$status" != "completed" ]; then
      echo "[merge-gate] poll #${poll_count}: '${c}' status=${status}"
      continue
    fi

    case "$conclusion" in
      success|skipped|neutral)
        check_status[i]="ok"
        echo "[merge-gate] poll #${poll_count}: '${c}' OK (${conclusion})"
        ;;
      *)
        check_status[i]="fail"
        echo "[merge-gate] poll #${poll_count}: '${c}' FAILED (${conclusion})"
        echo "::error title=Required check failed::'${c}' reported '${conclusion}'. See ${url}"
        # Fail fast: one failed check is enough to block the gate.
        exit 1
        ;;
    esac
  done

  if [ "$pending_count" -eq 0 ]; then
    echo "[merge-gate] all ${#checks[@]} check(s) completed successfully"
    exit 0
  fi

  sleep "$POLL_SEC"
done

# Timeout reached. Categorize what's missing vs stuck.
missing=()
stuck=()
for i in "${!checks[@]}"; do
  c="${checks[i]}"
  case "${check_status[i]}" in
    pending)
      if [ -z "${check_url[i]}" ]; then
        missing+=("$c")
      else
        stuck+=("$c")
      fi
      ;;
  esac
done

if [ "${#missing[@]}" -gt 0 ]; then
  {
    echo "::error title=Required check never started::The following check(s) did not appear for SHA ${SHA} within ${TIMEOUT_MIN} minutes:"
    for c in "${missing[@]}"; do echo "  - ${c}"; done
    echo ""
    echo "This usually indicates a transient GitHub Actions webhook delivery failure. Recovery:"
    echo "  1. Push an empty commit to retrigger:  git commit --allow-empty -m 'ci: retrigger' && git push"
    echo "  2. If that fails, close and reopen the PR."
    echo ""
    echo "Merge Gate catches this failure mode so it surfaces as a clear red check instead of a stuck 'Expected -- Waiting'. See .github/workflows/merge-gate.yml."
  } >&2
  exit 2
fi

{
  echo "::error title=Required check timeout::The following check(s) appeared but did not complete within ${TIMEOUT_MIN} minutes:"
  for i in "${!stuck[@]}"; do
    c="${stuck[i]}"
    # Find the original index to look up the URL.
    for j in "${!checks[@]}"; do
      if [ "${checks[$j]}" = "$c" ]; then
        echo "  - ${c} -> ${check_url[$j]}"
        break
      fi
    done
  done
} >&2
exit 3
