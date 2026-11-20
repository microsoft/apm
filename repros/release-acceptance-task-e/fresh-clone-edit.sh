#!/usr/bin/env bash
# Teammate case: clone a committed project (no receipts, no .apm-local), edit
# one local skill source, plain install. Exit 1 = refused although disk == lock.
set -u
APM_BIN_DIR=${APM_BIN_DIR:?}; export PATH=$APM_BIN_DIR:$PATH
for v in $(env | grep -oE '^(GIT_CONFIG_(COUNT|KEY_[0-9]+|VALUE_[0-9]+|SYSTEM|PARAMETERS)|GIT_AUTHOR_[A-Z]+|GIT_COMMITTER_[A-Z]+)='|tr -d '='); do unset "$v"; done
unset GIT_ASKPASS GITHUB_TOKEN GITHUB_APM_PAT GH_TOKEN CLAUDE_CONFIG_DIR HERMES_HOME APM_CACHE_DIR
T=$(mktemp -d); export HOME=$T/home APM_HOME=$T/home/.apm GIT_CONFIG_GLOBAL=$T/home/.gitconfig GIT_CONFIG_NOSYSTEM=1 APM_E2E_TESTS=1 XDG_CACHE_HOME=$T/home/.cache COPILOT_HOME=$T/home/.copilot
mkdir -p $APM_HOME $T/p; printf '[user]\n\tname = A\n\temail = a@example.invalid\n[commit]\n\tgpgsign = false\n' > $GIT_CONFIG_GLOBAL
cd $T/p && git init -q -b main && apm init -y --target copilot >/dev/null
mkdir -p .apm/skills/x/references
printf -- '---\nname: x\ndescription: Skill x.\n---\n# x\n' > .apm/skills/x/SKILL.md
printf 'reference a\n' > .apm/skills/x/references/a.md
apm install >/dev/null 2>&1; git add -A && git commit -qm A
export APM_HOME=$T/home2/.apm HOME=$T/home2; mkdir -p $APM_HOME; cp $T/home/.gitconfig $T/home2/.gitconfig; export GIT_CONFIG_GLOBAL=$T/home2/.gitconfig
git clone -q $T/p $T/c && cd $T/c
printf -- '---\nname: x\ndescription: Skill x.\n---\n# x\nedited\n' > .apm/skills/x/SKILL.md
echo '$ apm install   # fresh clone, fresh APM_HOME, only .apm/skills/x/SKILL.md edited'; apm install 2>&1 | sed 's/^/    /'; rc=${PIPESTATUS[0]}; echo "    rc=$rc"
git status --short | sed 's/^/    /'
[ $rc -ne 0 ] && { echo "REPRODUCED: refused with on-disk files equal to the lock"; exit 1; }; exit 0
