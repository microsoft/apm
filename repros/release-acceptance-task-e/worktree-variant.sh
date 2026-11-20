#!/usr/bin/env bash
# Variant 3: two git worktrees sharing one .git, one APM_HOME.
set -u
APM_BIN_DIR=${APM_BIN_DIR:?}; export PATH=$APM_BIN_DIR:$PATH
for v in $(env | grep -oE '^(GIT_CONFIG_(COUNT|KEY_[0-9]+|VALUE_[0-9]+|SYSTEM|PARAMETERS)|GIT_AUTHOR_[A-Z]+|GIT_COMMITTER_[A-Z]+)='|tr -d '='); do unset "$v"; done
unset GIT_ASKPASS GITHUB_TOKEN GITHUB_APM_PAT GH_TOKEN CLAUDE_CONFIG_DIR HERMES_HOME APM_CACHE_DIR
T=$(mktemp -d); export HOME=$T/home APM_HOME=$T/home/.apm GIT_CONFIG_GLOBAL=$T/home/.gitconfig GIT_CONFIG_NOSYSTEM=1 APM_E2E_TESTS=1 XDG_CACHE_HOME=$T/home/.cache COPILOT_HOME=$T/home/.copilot
mkdir -p $APM_HOME $T/wt1; printf '[user]\n\tname = A\n\temail = a@example.invalid\n[commit]\n\tgpgsign = false\n' > $GIT_CONFIG_GLOBAL
D=.agents/skills/x
run() { echo "\$ (cd $(basename $PWD)) $*"; "$@" 2>&1 | sed 's/^/    /'; echo "    rc=${PIPESTATUS[0]}"; }
st() { echo "--- $1 ($(basename $PWD), $(git rev-parse --abbrev-ref HEAD)):"; git status --short | sed 's/^/    /'
  for f in SKILL.md references/a.md; do d=$([ -f $D/$f ] && sha256sum < $D/$f | cut -c1-12 || echo MISSING); l=$(grep -m1 "^  $D/$f: sha256:" apm.lock.yaml | awk '{print $2}' | cut -c8-19); echo "    $f disk=$d lock=${l:-NONE} src=$([ -f .apm/skills/x/$f ] && echo present || echo MISSING)"; done; }
cd $T/wt1 && git init -q -b main && apm init -y --target copilot >/dev/null
mkdir -p .apm/skills/x/references
printf -- '---\nname: x\ndescription: Skill x.\n---\n# x\n' > .apm/skills/x/SKILL.md
printf 'reference a, version A\n' > .apm/skills/x/references/a.md
run apm install; git add -A && git commit -qm A; st "wt1 A committed"
git worktree add -q -b B $T/wt2; cd $T/wt2; st "wt2 B fresh worktree"
run apm install; st "wt2 plain install, no edits"
printf 'reference a, version B\n' > .apm/skills/x/references/a.md
printf -- '---\nname: x\ndescription: Skill x.\n---\n# x\n(B)\n' > .apm/skills/x/SKILL.md
run apm install; st "wt2 after editing both sources"
git add -A && git commit -qm B
cd $T/wt1; run apm install; st "wt1 plain install after wt2 work"
printf -- '---\nname: x\ndescription: Skill x.\n---\n# x\n(A2)\n' > .apm/skills/x/SKILL.md
run apm install; st "wt1 after SKILL.md-only edit"
echo "--- receipts in shared APM_HOME:"; ls $APM_HOME/deployment-receipts/ | sed 's/^/    /'
for w in wt1 wt2; do echo "    $w key=$(printf '%s|project' "$(cd $T/$w && pwd -P)" | sha256sum | cut -c1-16)"; done
