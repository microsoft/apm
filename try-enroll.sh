#!/usr/bin/env bash
# Interactive walkthrough: try `apm enroll` against real GitHub and GitLab
# marketplaces, and report what your machine's credentials actually do.
#
# Unlike validate-enroll.sh (offline, sandboxed, pass/fail), this one makes
# real network calls and is meant to be READ -- it shows you the resolution
# each host takes so you can judge the behaviour yourself.
#
#   APM_BIN=/path/to/venv/bin/apm ./try-enroll.sh
#
# Uses a sandboxed HOME so your real ~/.apm/marketplaces.json is untouched.
# Because that also hides ~/.config/gh and glab's config, tokens are read
# from both CLIs *before* the swap and passed in explicitly.
set -uo pipefail

APM="${APM_BIN:-}"
[ -n "$APM" ] || { echo "Set APM_BIN to the apm built from this branch."; exit 2; }

GH_SOURCE="${GH_SOURCE:-github/awesome-copilot}"
GL_SOURCE="${GL_SOURCE:-}"

# Read the CLIs' credentials BEFORE sandboxing HOME: gh reads ~/.config/gh
# and glab reads ~/Library/Application Support/glab-cli, so a swapped HOME
# hides both and every lookup below would come back empty.
GH_TOK="$(gh auth token 2>/dev/null || true)"
# There is no `glab auth token` subcommand -- it prints help text to stdout,
# which silently becomes a 1400-char "token" if captured. Use the credential
# helper instead.
GL_TOK="$(printf 'protocol=https\nhost=gitlab.com\n\n' \
  | glab auth git-credential get 2>/dev/null | sed -n 's/^password=//p')"
ENV_GL_PAT="${GITLAB_APM_PAT:-}"

SANDBOX="$(mktemp -d)"; trap 'rm -rf "$SANDBOX"' EXIT
export HOME="$SANDBOX"

hdr() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
note() { printf '   \033[2m%s\033[0m\n' "$1"; }

hdr "Binary"
"$APM" --version 2>&1 | head -1

# ---------------------------------------------------------------- GitHub ---
hdr "GitHub: what credential does your machine offer?"
if [ -n "$GH_TOK" ]; then
  note "gh CLI has a token. APM resolves GitHub via 'gh auth token' natively,"
  note "so unsandboxed you would not need to set anything."
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    -H "Authorization: token $GH_TOK" https://api.github.com/user 2>/dev/null)
  note "GitHub API accepts it: HTTP $code"
else
  note "No gh token found. Run 'gh auth login', or set GITHUB_APM_PAT."
fi

hdr "GitHub: enroll on $GH_SOURCE"
GITHUB_APM_PAT="$GH_TOK" "$APM" enroll "$GH_SOURCE" --name try-gh </dev/null \
  >"$SANDBOX/gh.out" 2>&1
gh_code=$?
head -12 "$SANDBOX/gh.out"
note "exit: $gh_code"

# ---------------------------------------------------------------- GitLab ---
hdr "GitLab: what credential does your machine offer?"
if [ -n "$GL_TOK" ]; then
  pt=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    -H "PRIVATE-TOKEN: $GL_TOK" https://gitlab.com/api/v4/user 2>/dev/null)
  br=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    -H "Authorization: Bearer $GL_TOK" https://gitlab.com/api/v4/user 2>/dev/null)
  note "glab credential -> PRIVATE-TOKEN: $pt | Bearer: $br"
  if [ "$pt" != "200" ] && [ "$br" = "200" ]; then
    printf '   \033[33m%s\033[0m\n' "This is an OAuth session token, not a PAT."
    note "APM sends PRIVATE-TOKEN, which OAuth tokens do not satisfy -- so this"
    note "credential will NOT work for a GitLab marketplace. Create a real PAT"
    note "(scopes read_repository,read_api); 'apm enroll' will walk you through"
    note "it interactively, or set GITLAB_APM_PAT yourself."
  fi
else
  note "No glab credential found. Run 'glab auth login', or set GITLAB_APM_PAT."
fi

if [ -z "$GL_SOURCE" ]; then
  hdr "GitLab: skipped"
  note "Set GL_SOURCE to a GitLab marketplace to test, e.g.:"
  note "  GL_SOURCE=gitlab.com/group/sub/apm-marketplace $0"
else
  hdr "GitLab: enroll on $GL_SOURCE"
  note "Using GITLAB_APM_PAT from your environment if set, else the glab credential."
  GITLAB_APM_PAT="${ENV_GL_PAT:-$GL_TOK}" \
    "$APM" enroll "$GL_SOURCE" --name try-gl </dev/null >"$SANDBOX/gl.out" 2>&1
  gl_code=$?
  head -12 "$SANDBOX/gl.out"
  note "exit: $gl_code"
fi

hdr "Your real config"
real="$(eval echo ~"$(whoami)")/.apm/marketplaces.json"
grep -q "try-gh\|try-gl" "$real" 2>/dev/null \
  && printf '   \033[31m%s\033[0m\n' "LEAKED into $real" \
  || note "untouched: $real"

hdr "Reading the output"
note "'Using <host> credential from <source>'  -> a token was found (not validated)"
note "'Continuing without a credential'        -> none found; public still works"
note "'No marketplace.json found ... Checked'  -> can mean BAD AUTH, not a missing"
note "                                            file: GitHub/GitLab 404 a private"
note "                                            repo rather than admit it exists."
