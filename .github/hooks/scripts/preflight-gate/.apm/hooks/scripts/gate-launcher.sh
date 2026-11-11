#!/usr/bin/env bash
# preflight-gate hook launcher (preToolUse).
#
# MUST NEVER FAIL: a non-zero exit from a preToolUse command hook denies the
# tool call, so a broken launcher would brick every tool. Every path below
# exits 0; only a well-formed deny JSON printed by gate.py denies.
#
# Fast path: tool calls whose payload mentions none of the gated verbs are
# released without starting Python.
payload="$(cat 2>/dev/null)" || payload=""
case "$payload" in
  *push*|*pull*|*'gh '*|*'"gh'*|*'pr '*|*'git '*|*'"git'*) ;;
  *) exit 0 ;;
esac
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || exit 0
[ -f "$dir/gate.py" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
printf '%s' "$payload" | python3 -B "$dir/gate.py" || true
exit 0
