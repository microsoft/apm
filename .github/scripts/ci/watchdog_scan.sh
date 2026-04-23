#!/usr/bin/env bash
# watchdog_scan.sh -- scan open PRs for the "stuck pull_request webhook"
# failure mode and post a single recovery comment per stuck PR.
#
# Used by .github/workflows/watchdog-stuck-prs.yml on a 15-minute cron.
# Acts as a safety net while merge-gate.yml rolls out (and remains useful
# afterwards as a backstop for any required check that stops dispatching).
#
# Detection rule:
#   For each open PR not in draft:
#     - take last-update age in minutes (proxy for "time since last push")
#     - if age is in the [WATCHDOG_MIN_AGE_MIN, WATCHDOG_MAX_AGE_MIN] window
#     - AND the head SHA has zero check-runs named EXPECTED_CHECK
#   then post the recovery comment exactly once (idempotency via a marker
#   string `WATCHDOG_STUCK_WEBHOOK_MARKER` embedded in the comment body).
#
# Inputs (environment variables):
#   GH_TOKEN               required. Token with pull-requests:write, checks:read.
#   REPO                   required. owner/repo.
#   EXPECTED_CHECK         optional. Default: "Build & Test (Linux)".
#   WATCHDOG_MIN_AGE_MIN   optional. Default: 15.
#   WATCHDOG_MAX_AGE_MIN   optional. Default: 1440 (1 day).
#   WATCHDOG_DRY_RUN       optional. If "1", log decisions but post no comment.
#
# Exit code is always 0 unless the script itself errors -- one stuck PR
# should not fail the whole scan.

set -euo pipefail

EXPECTED_CHECK="${EXPECTED_CHECK:-Build & Test (Linux)}"
MIN_AGE_MIN="${WATCHDOG_MIN_AGE_MIN:-15}"
MAX_AGE_MIN="${WATCHDOG_MAX_AGE_MIN:-1440}"
DRY_RUN="${WATCHDOG_DRY_RUN:-0}"
MARKER="WATCHDOG_STUCK_WEBHOOK_MARKER"

if [ -z "${GH_TOKEN:-}" ] || [ -z "${REPO:-}" ]; then
  echo "ERROR: GH_TOKEN and REPO are required." >&2
  exit 1
fi

now_ts=$(date -u +%s)

# Use updated_at as the "last activity" proxy. headRefOid changes on every
# push; updated_at changes on every push, comment, label etc. The combination
# is fine for this safety-net heuristic.
prs_stderr=$(mktemp)
if ! prs=$(gh pr list --repo "$REPO" --state open --limit 200 \
  --json number,headRefOid,updatedAt,isDraft,title 2>"$prs_stderr"); then
  echo "::error title=Watchdog scan failed::Could not list open PRs in ${REPO}. Stderr below." >&2
  cat "$prs_stderr" >&2
  rm -f "$prs_stderr"
  exit 1
fi
rm -f "$prs_stderr"

count_total=$(echo "$prs" | jq 'length')
count_stuck=0
count_commented=0

echo "[watchdog] scanning ${count_total} open PRs in ${REPO}"
echo "[watchdog] expected_check='${EXPECTED_CHECK}' window=[${MIN_AGE_MIN}m..${MAX_AGE_MIN}m] dry_run=${DRY_RUN}"

while read -r pr; do
  number=$(echo "$pr" | jq -r '.number')
  sha=$(echo "$pr" | jq -r '.headRefOid')
  updated=$(echo "$pr" | jq -r '.updatedAt')
  is_draft=$(echo "$pr" | jq -r '.isDraft')
  title=$(echo "$pr" | jq -r '.title')

  if [ "$is_draft" = "true" ]; then
    continue
  fi

  # Portable date parse (works on macOS BSD date and GNU date).
  if updated_ts=$(date -u -d "$updated" +%s 2>/dev/null); then
    :
  elif updated_ts=$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$updated" +%s 2>/dev/null); then
    :
  else
    echo "[watchdog] PR #${number}: cannot parse updated_at='${updated}', skipping" >&2
    continue
  fi

  age_min=$(( (now_ts - updated_ts) / 60 ))

  if [ "$age_min" -lt "$MIN_AGE_MIN" ] || [ "$age_min" -gt "$MAX_AGE_MIN" ]; then
    continue
  fi

  present=$(gh api \
    -H "Accept: application/vnd.github+json" \
    "repos/${REPO}/commits/${sha}/check-runs?check_name=$(jq -rn --arg n "$EXPECTED_CHECK" '$n|@uri')&per_page=1" \
    --jq '.check_runs | length' 2>/dev/null || echo "0")

  if [ "$present" != "0" ]; then
    continue
  fi

  # Distinguish "stuck webhook" from "legitimately skipped":
  # 1. If ci.yml has ANY workflow run for this SHA, it dispatched (good).
  workflow_runs=$(gh api \
    -H "Accept: application/vnd.github+json" \
    "repos/${REPO}/actions/workflows/ci.yml/runs?head_sha=${sha}&per_page=1" \
    --jq '.total_count' 2>/dev/null || echo "0")

  if [ "$workflow_runs" != "0" ]; then
    continue
  fi

  # 2. If all changed files match ci.yml's paths-ignore, the workflow was
  #    correctly suppressed and no run is expected.
  #    Keep this list in sync with .github/workflows/ci.yml `paths-ignore`.
  #    Use --paginate to handle PRs touching >100 files.
  non_ignored=$(gh api --paginate \
    -H "Accept: application/vnd.github+json" \
    "repos/${REPO}/pulls/${number}/files" \
    --jq '.[].filename' 2>/dev/null \
    | awk '!/^docs\// && $0 != ".gitignore" && $0 != "LICENSE"' \
    | wc -l \
    | tr -d ' ')
  case "$non_ignored" in ''|*[!0-9]*) non_ignored=0 ;; esac

  if [ "$non_ignored" = "0" ]; then
    continue
  fi

  count_stuck=$((count_stuck + 1))
  echo "[watchdog] PR #${number} ('${title}') looks stuck: age=${age_min}m sha=${sha} non_ignored_files=${non_ignored}"

  # Idempotency: do not double-comment.
  has_comment=$(gh api \
    -H "Accept: application/vnd.github+json" \
    "repos/${REPO}/issues/${number}/comments?per_page=100" \
    --jq "[.[] | select(.body | contains(\"${MARKER}\"))] | length" 2>/dev/null || echo "0")

  if [ "$has_comment" != "0" ]; then
    echo "[watchdog] PR #${number}: already commented, skipping"
    continue
  fi

  if [ "$DRY_RUN" = "1" ]; then
    echo "[watchdog] PR #${number}: DRY_RUN, would have commented"
    continue
  fi

  sha_short="${sha:0:8}"
  body_file=$(mktemp)
  # shellcheck disable=SC2016  # backticks here are markdown, not command subst
  {
    printf '<!-- %s -->\n' "$MARKER"
    printf ':warning: **Stuck CI webhook detected**\n\n'
    printf 'This PR'\''s head commit (`%s`) has no `%s` check-run after %s minutes. ' "$sha_short" "$EXPECTED_CHECK" "$age_min"
    printf 'This usually means a GitHub Actions webhook delivery for the `pull_request` event was dropped and never recovered.\n\n'
    printf '**Recovery:** push an empty commit to retrigger:\n\n'
    printf '```bash\n'
    printf 'git commit --allow-empty -m '\''ci: retrigger'\''\n'
    printf 'git push\n'
    printf '```\n\n'
    printf 'If that does not help, close and reopen the PR.\n\n'
    printf 'This comment is posted by `.github/workflows/watchdog-stuck-prs.yml`. It will not be repeated for this PR. See `.github/workflows/merge-gate.yml` for the orchestrator that aims to make this failure mode self-healing.\n'
  } > "$body_file"

  if gh pr comment "$number" --repo "$REPO" --body-file "$body_file" >/dev/null 2>&1; then
    count_commented=$((count_commented + 1))
    echo "[watchdog] PR #${number}: comment posted"
  else
    echo "[watchdog] PR #${number}: failed to post comment" >&2
  fi
  rm -f "$body_file"
done < <(echo "$prs" | jq -c '.[]')

echo "[watchdog] done. stuck=${count_stuck} commented=${count_commented}"
