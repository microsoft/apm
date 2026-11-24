#!/usr/bin/env bash
# Back up perf-report to origin (sessions can be deleted). Orphan branch, no workflows.
set -euo pipefail
cd "$(dirname "$0")"
BR="${PERF_REPORT_BRANCH:-perf-report/ceo-2026-11-25}"
[ -d .git ] || { git init -q; git checkout -q -b "$BR"; }
git add -A
git -c user.name="perf-report-writer" -c user.email="perf-report@users.noreply.github.com" \
  commit -q -m "perf-report: snapshot $(date '+%Y-%m-%d %H:%M %Z')" || echo "[i] nothing new to commit"
git push -q --force https://github.com/microsoft/apm.git "HEAD:refs/heads/$BR"
echo "[+] pushed $(git rev-parse --short HEAD) to $BR"
