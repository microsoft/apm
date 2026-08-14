#!/usr/bin/env bash
# Local validation for `apm enroll` (feature/enroll-command).
#
# Runs every check that does NOT need a private-repo credential, in a
# sandboxed HOME so your real ~/.apm/marketplaces.json is never touched.
#
# Usage:
#   ./validate-enroll.sh            # offline checks only
#   ./validate-enroll.sh <SOURCE>   # also try a real marketplace (interactive)
set -uo pipefail

APM="${APM_BIN:-}"
if [ -z "$APM" ]; then
  echo "Set APM_BIN to the apm built from this branch, e.g."
  echo "  APM_BIN=/path/to/venv/bin/apm $0"
  exit 2
fi

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
export HOME="$SANDBOX"          # isolates ~/.apm -- CONFIG_DIR is hardcoded to ~/.apm

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

head_ "0. Which binary am I testing?"
echo "  $("$APM" --version 2>&1 | head -1)"
echo "  sandboxed HOME: $HOME"

head_ "1. Command is registered and documented"
"$APM" enroll --help >/dev/null 2>&1 && ok "apm enroll --help works" || bad "enroll not registered"
"$APM" --help 2>/dev/null | grep -q "enroll" && ok "listed in apm --help" || bad "missing from apm --help"

head_ "2. Happy path -- local marketplace, no credentials needed"
mkdir -p "$SANDBOX/mkt/.claude-plugin"
cat > "$SANDBOX/mkt/.claude-plugin/marketplace.json" <<'JSON'
{"name":"local-demo","owner":{"name":"you"},
 "plugins":[{"name":"hello-plugin","source":"./hello","description":"Validation plugin","version":"1.0.0"}]}
JSON
if "$APM" enroll "$SANDBOX/mkt" --name demo </dev/null >"$SANDBOX/out1" 2>&1; then
  ok "registered and browsed (exit 0)"
  grep -q "hello-plugin" "$SANDBOX/out1" && ok "plugin listed in browse output" || bad "browse did not list the plugin"
else
  bad "happy path failed -- output:"; sed 's/^/      /' "$SANDBOX/out1"
fi

head_ "3. Idempotent -- safe to re-run"
"$APM" enroll "$SANDBOX/mkt" --name demo </dev/null >/dev/null 2>&1 \
  && ok "re-run still exits 0" || bad "re-run failed"

head_ "4. Alias recovery -- no --name given"
if "$APM" enroll "$SANDBOX/mkt" </dev/null >"$SANDBOX/out2" 2>&1; then
  grep -q "local-demo" "$SANDBOX/out2" \
    && ok "derived alias 'local-demo' from the manifest" \
    || bad "did not recover the registered alias"
else
  bad "no --name run failed"
fi

head_ "5. CI guard -- must NOT hang; warns, names the env var, defers to registration"
# Subshell rather than `env -u`: macOS env requires -u before assignments,
# GNU env does not. A subshell is portable across both.
(
  unset GITLAB_APM_PAT GITLAB_TOKEN
  export APM_NON_INTERACTIVE=1
  "$APM" enroll "gitlab.com/no-such-org-xyz/private-repo" --name ci </dev/null
) >"$SANDBOX/out3" 2>&1
code=$?
# A missing credential is no longer fatal on its own -- a public marketplace
# needs none. This source does not exist, so registration fails and exit is 1.
[ "$code" -eq 1 ] && ok "exited 1 via registration (no hang)" || bad "expected exit 1, got $code"
grep -q "GITLAB_APM_PAT" "$SANDBOX/out3" && ok "names the env var to set" || bad "no actionable guidance"
grep -qi "paste the token" "$SANDBOX/out3" && bad "prompted despite APM_NON_INTERACTIVE" || ok "never prompted"

head_ "6. Host-specific credential handling (offline, no API calls)"
PY_BIN="$(dirname "$APM")/python"
if [ -x "$PY_BIN" ]; then
  probe=$("$PY_BIN" - <<'PY' 2>&1
from apm_cli.commands.enroll import _token_env_var, _token_scopes, _token_page_url
print(_token_env_var("github"), _token_scopes("github"), sep="|")
print(_token_page_url("ghe.corp.example", "ghes", "t"))
PY
)
  case "$probe" in
    *"GITHUB_APM_PAT|repo"*) ok "GitHub -> GITHUB_APM_PAT, scope 'repo'" ;;
    *) bad "GitHub credential mapping wrong: $probe" ;;
  esac
  case "$probe" in
    # Hardcoding api.github.com / github.com would break Enterprise Server.
    *"https://ghe.corp.example/settings/tokens/new"*) ok "GHES token page stays on the enterprise host" ;;
    *) bad "GHES token page wrong: $probe" ;;
  esac
else
  echo "  SKIP  no python next to $APM"
fi

head_ "7. Rejects bad input"
"$APM" enroll "http://gitlab.com/a/b" --name x </dev/null >/dev/null 2>&1
[ $? -ne 0 ] && ok "insecure HTTP rejected" || bad "accepted an http:// source"

head_ "8. Your real config was never touched"
real="$(eval echo ~"$(whoami)")/.apm/marketplaces.json"
if [ -f "$real" ] && ! grep -q '"name": *"demo"' "$real" 2>/dev/null; then
  ok "no sandbox entries leaked into $real"
else
  [ -f "$real" ] || ok "no real config present to touch"
fi

if [ $# -ge 1 ]; then
  head_ "9. Real marketplace: $1  (interactive -- may prompt for a token)"
  echo "  This is the path I could not test: browser + paste against a private remote."
  "$APM" enroll "$1" --name validate-real
  echo "  exit code: $?"
fi

printf '\n\033[1mResult: %d passed, %d failed\033[0m\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
