#!/usr/bin/env bash
# Single worktree, no branch switch. CASE selects the edit:
#  R1: edit deployed SKILL.md + edit source references/a.md
#  R2: edit source SKILL.md + edit deployed SKILL.md (different content)
#  R3: edit deployed SKILL.md only
#  R4: edit deployed SKILL.md + edit source SKILL.md + edit source a.md
#  R5: edit source a.md, install, discard all changes (git checkout -- .), edit source SKILL.md
#  R6: like R5 but discard with git stash
set -u
APM_BIN_DIR=${APM_BIN_DIR:?}; CASE=${CASE:?}; export PATH=$APM_BIN_DIR:$PATH
for v in $(env | grep -oE '^(GIT_CONFIG_(COUNT|KEY_[0-9]+|VALUE_[0-9]+|SYSTEM|PARAMETERS)|GIT_AUTHOR_[A-Z]+|GIT_COMMITTER_[A-Z]+)='|tr -d '='); do unset "$v"; done
unset GIT_ASKPASS GITHUB_TOKEN GITHUB_APM_PAT GH_TOKEN CLAUDE_CONFIG_DIR HERMES_HOME APM_CACHE_DIR
T=$(mktemp -d); export HOME=$T/home APM_HOME=$T/home/.apm GIT_CONFIG_GLOBAL=$T/home/.gitconfig GIT_CONFIG_NOSYSTEM=1 APM_E2E_TESTS=1 XDG_CACHE_HOME=$T/home/.cache COPILOT_HOME=$T/home/.copilot
mkdir -p $APM_HOME $T/p; printf '[user]\n\tname = A\n\temail = a@example.invalid\n[commit]\n\tgpgsign = false\n' > $GIT_CONFIG_GLOBAL
cd $T/p && git init -q -b main && apm init -y --target copilot >/dev/null
D=.agents/skills/x; S=.apm/skills/x
mkdir -p $S/references
printf -- '---\nname: x\ndescription: Skill x.\n---\n# x\n' > $S/SKILL.md
printf 'reference a\n' > $S/references/a.md
apm install >/dev/null 2>&1; git add -A && git commit -qm A
h() { [ -f "$1" ] && sha256sum < "$1" | cut -c1-12 || echo MISSING; }
lk() { grep -m1 "^  $1: sha256:" apm.lock.yaml | awk '{print $2}' | cut -c8-19; }
case $CASE in
  R1) echo "USER EDIT" >> $D/SKILL.md; printf 'reference a v2\n' > $S/references/a.md;;
  R2) printf -- '---\nname: x\ndescription: Skill x.\n---\n# x\nsource edit\n' > $S/SKILL.md; echo "USER EDIT" >> $D/SKILL.md;;
  R3) echo "USER EDIT" >> $D/SKILL.md;;
  R4) echo "USER EDIT" >> $D/SKILL.md; printf -- '---\nname: x\ndescription: Skill x.\n---\n# x\nsource edit\n' > $S/SKILL.md; printf 'reference a v2\n' > $S/references/a.md;;
  R5|R6) printf 'reference a v2\n' > $S/references/a.md; echo "\$ apm install   # deploy a.md v2 (uncommitted)"; apm install 2>&1 | tail -1 | sed 's/^/    /'
      if [ $CASE = R5 ]; then echo "\$ git checkout -- ."; git checkout -- .; else echo "\$ git stash"; git stash -q; fi
      git status --short | sed 's/^/    /'; printf -- '---\nname: x\ndescription: Skill x.\n---\n# x\nsource edit\n' > $S/SKILL.md;;
esac
echo "before: SKILL.md disk=$(h $D/SKILL.md) lock=$(lk $D/SKILL.md) | a.md disk=$(h $D/references/a.md) lock=$(lk $D/references/a.md) src=$( [ -f $S/references/a.md ] && echo present)"
echo "\$ apm install"; apm install 2>&1 | sed 's/^/    /'; echo "    rc=${PIPESTATUS[0]}"
git status --short | sed 's/^/    /'
echo "after:  SKILL.md disk=$(h $D/SKILL.md) | a.md disk=$(h $D/references/a.md) lock=$(lk $D/references/a.md) | user edit kept: $(grep -q 'USER EDIT' $D/SKILL.md 2>/dev/null && echo yes || echo no)"
if [ "${RECOVER:-0}" = 1 ]; then echo "\$ apm install   # second plain install"; apm install 2>&1 | sed 's/^/    /'; echo "    after 2nd install: a.md disk=$(h $D/references/a.md) lock=$(lk $D/references/a.md)"; fi
[ -f $D/references/a.md ] || { echo "DATA LOSS: deployed references/a.md deleted while its source exists"; exit 1; }
exit 0
