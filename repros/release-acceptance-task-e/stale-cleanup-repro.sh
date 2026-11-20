#!/usr/bin/env bash
# Hermetic repro: does a plain `apm install` delete a deployed file whose source
# still exists, or refuse with "no trustworthy local deployment baseline" when
# on-disk files match the lock, after switching branches?
# usage: APM_BIN_DIR=<dir with apm> VARIANT=clean|dirty bash stale-cleanup-repro.sh
set -u
APM_BIN_DIR=${APM_BIN_DIR:?set APM_BIN_DIR}; VARIANT=${VARIANT:-clean}
export PATH=$APM_BIN_DIR:$PATH
for v in $(env | grep -oE '^(GIT_CONFIG_(COUNT|KEY_[0-9]+|VALUE_[0-9]+|SYSTEM|PARAMETERS)|GIT_AUTHOR_[A-Z]+|GIT_COMMITTER_[A-Z]+|GITHUB_APM_PAT_[A-Z0-9_]+)='|tr -d '='); do unset "$v"; done
unset GIT_ASKPASS GIT_SSH_COMMAND GIT_PROXY_COMMAND GITHUB_TOKEN GITHUB_APM_PAT GH_TOKEN ADO_APM_PAT CLAUDE_CONFIG_DIR HERMES_HOME APM_CACHE_DIR
T=$(mktemp -d); export HOME=$T/home APM_HOME=$T/home/.apm GIT_CONFIG_GLOBAL=$T/home/.gitconfig GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 APM_E2E_TESTS=1 XDG_CONFIG_HOME=$T/home/.config XDG_CACHE_HOME=$T/home/.cache COPILOT_HOME=$T/home/.copilot CODEX_HOME=$T/home/.codex
mkdir -p $APM_HOME $T/p; printf '[user]\n\tname = A\n\temail = a@example.invalid\n[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n' > $GIT_CONFIG_GLOBAL
cd $T/p
D=.agents/skills/x
run() { echo "\$ $*"; "$@" 2>&1 | sed 's/^/    /'; echo "    rc=${PIPESTATUS[0]}"; }
state() {
  echo "--- state ($1): branch=$(git rev-parse --abbrev-ref HEAD) status:"; git status --short | sed 's/^/    /'
  for f in SKILL.md references/a.md; do
    if [ -f $D/$f ]; then disk=sha256:$(sha256sum < $D/$f | cut -d' ' -f1); else disk=MISSING; fi
    lock=$(grep -m1 "^  $D/$f: sha256:" apm.lock.yaml | awk '{print $2}')
    src=$( [ -f .apm/skills/x/$f ] && echo present || echo MISSING )
    echo "    $D/$f disk=${disk:0:19} lock=${lock:0:19} $( [ "$disk" = "$lock" ] && echo MATCH || echo DIFF ) source=$src"
  done
}
git init -q -b main
apm init -y --target copilot >/dev/null
mkdir -p .apm/skills/x/references
printf -- '---\nname: x\ndescription: Skill x for the repro.\n---\n# x\nRead references/a.md.\n' > .apm/skills/x/SKILL.md
printf 'reference a, version A\n' > .apm/skills/x/references/a.md
run apm install
git add -A && git commit -qm "A: deploy skill x"; state "A committed"
git checkout -q -b B
printf 'reference a, version B\n' > .apm/skills/x/references/a.md
printf -- '---\nname: x\ndescription: Skill x for the repro.\n---\n# x\nRead references/a.md (B).\n' > .apm/skills/x/SKILL.md
run apm install
if [ "$VARIANT" = clean ]; then
  git add -A && git commit -qm "B: edit and redeploy"
else
  # dirty: commit only the sources; deployed files and lock stay modified in the tree
  git add .apm && git commit -qm "B: edit sources only"
fi
state "B after install"
echo "===== checkout A ($VARIANT)"; git checkout -q main 2>&1; state "A after checkout"
run apm install; state "A after plain install"
echo "===== checkout B ($VARIANT)"; git checkout -q B 2>&1; state "B after checkout"
run apm install; state "B after plain install"
echo "===== back to A, edit only SKILL.md source (mirrors the lane sequence)"
git stash -q -u 2>/dev/null; git checkout -q main; git stash drop -q 2>/dev/null; state "A clean"
if [ -n "${PRE_FINAL:-}" ]; then echo "\$ $PRE_FINAL"; eval "$PRE_FINAL" 2>&1 | sed 's/^/    /'; fi
printf -- '---\nname: x\ndescription: Skill x for the repro.\n---\n# x\nRead references/a.md (A2).\n' > .apm/skills/x/SKILL.md
run apm install; state "A after SKILL.md-only edit + plain install"
