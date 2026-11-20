#!/usr/bin/env bash
# Control: no branch switching. Deploy a local skill, commit, edit only the
# source SKILL.md, run a plain install. Exit 1 = references/a.md deleted.
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
[ "${SECOND_INSTALL:-0}" = 1 ] && { apm install > /dev/null 2>&1; echo "(extra plain install before the edit)"; }
printf -- '---\nname: x\ndescription: Skill x.\n---\n# x\nedited\n' > .apm/skills/x/SKILL.md
echo '$ apm install   # after editing only .apm/skills/x/SKILL.md'; apm install 2>&1 | sed 's/^/    /'; echo "    rc=${PIPESTATUS[0]}"
git status --short | sed 's/^/    /'
[ -f .agents/skills/x/references/a.md ] && { echo "references/a.md present"; exit 0; }
echo "REPRODUCED: deployed references/a.md deleted while .apm/skills/x/references/a.md exists"; exit 1
