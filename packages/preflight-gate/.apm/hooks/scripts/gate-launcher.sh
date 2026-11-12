#!/usr/bin/env bash
# preflight-gate hook launcher (preToolUse).
#
# MUST NEVER FAIL: a non-zero exit from a preToolUse command hook denies the
# tool call, so a broken launcher would brick every tool. Every path below
# exits 0; only a well-formed deny JSON printed by gate.py denies.
#
# Fast path (pure shell, no subprocess): Python starts only for payloads that
# can publish -- a create_pull_request tool, an update_pull_request tool that
# touches draft/ready, any text containing `push`, or `gh` together with
# pr / pulls / createPullRequest / markPullRequestReadyForReview. Everything
# else exits 0 here.
payload=""
IFS= read -r -d '' payload 2>/dev/null || true
publish=""
# Each pattern is checked on its own: a tool name mentioned in some argument
# (e.g. a PR title) must never mask a gh / push publish in the same payload.
case "$payload" in
  *create_pull_request*|*push*) publish=1 ;;
esac
case "$payload" in
  *update_pull_request*)
    case "$payload" in *draft*|*ready*|*Ready*) publish=1 ;; esac ;;
esac
case "$payload" in
  *'gh '*|*'gh\t'*)
    case "$payload" in
      *'pr '*|*'pr\t'*|*'pr"'*|*pulls*|*createPullRequest*|*markPullRequestReadyForReview*)
        publish=1 ;;
    esac ;;
esac
[ -n "$publish" ] || exit 0
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || exit 0
[ -f "$dir/gate.py" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
printf '%s' "$payload" | python3 -B "$dir/gate.py" || true
exit 0
