# Restore Release Community Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore complete, issue-linked coverage for every community-visible fix shipped by PR #2155 in the v0.25.0 release notes.

**Architecture:** Treat GitHub PR #2155 `closingIssuesReferences` as the closure source of truth. Replace the broad changelog summary with 11 user-outcome groups, validate exact set equality for all 24 issue numbers, then synchronize PR #2164's description before pushing.

**Tech Stack:** Markdown, GitHub CLI, Python standard library, existing cut-release lint scripts

---

## File structure

- Modify: `CHANGELOG.md` - replace the broad #2155 summary with grouped,
  issue-linked user outcomes.
- Modify: `/Users/danielmeppiel/.copilot/session-state/1124f104-3167-48e8-a6cb-37c77797be67/files/pr-body-v0.25.0.md`
  - keep PR #2164's counts, rationale, and trade-offs truthful.
- Read: `docs/superpowers/specs/2026-07-12-release-changelog-community-fixes-design.md`
  - authoritative grouping and validation contract.

### Task 1: Restore #2155 issue coverage in the changelog

**Files:**
- Modify: `CHANGELOG.md:20-67`
- Reference: `docs/superpowers/specs/2026-07-12-release-changelog-community-fixes-design.md`

- [ ] **Step 1: Run the closure validator against the current changelog**

Run:

```bash
uv run python - <<'PY'
import json
import re
import subprocess
from pathlib import Path

payload = json.loads(
    subprocess.check_output(
        ["gh", "pr", "view", "2155", "--json", "closingIssuesReferences"],
        text=True,
    )
)
expected = {item["number"] for item in payload["closingIssuesReferences"]}
text = Path("CHANGELOG.md").read_text(encoding="utf-8")
release = text.split("## [0.25.0]", 1)[1].split("## [0.24.1]", 1)[0]
entries = re.findall(r"(?ms)^- .*?(?=^- |^### |^## |\Z)", release)
actual = []
for entry in entries:
    if "#2155" not in entry or "(closes " not in entry:
        continue
    closure_text = entry.split("(closes ", 1)[1].split(";", 1)[0]
    actual.extend(int(value) for value in re.findall(r"#(\d+)", closure_text))

duplicates = {value for value in actual if actual.count(value) > 1}
assert len(actual) == 24, f"expected 24 closure references, found {len(actual)}"
assert not duplicates, f"duplicate closure references: {sorted(duplicates)}"
assert set(actual) == expected, (
    f"missing={sorted(expected - set(actual))}, "
    f"extra={sorted(set(actual) - expected)}"
)
print("validated 24 unique #2155 issue closures")
PY
```

Expected: FAIL with `expected 24 closure references, found 0`.

- [ ] **Step 2: Replace the broad #2155 summary with the approved Changed entries**

Use these exact entries under `### Changed`:

```markdown
- `apm compile --target`, compile help and errors, and `apm init --target`
  now use one canonical target catalog, so every advertised target is accepted
  consistently. (closes #2138, #2147; #2155)
- Generated hooks now use canonical upstream contracts: Claude matcher/hooks
  nesting, Kiro v1 schema, Copilot's required top-level version, and provenance
  outside vendor payloads. (closes #2062, #2071, #2128, #2157; #2155)
```

Keep the existing Homebrew entry immediately after these entries.

- [ ] **Step 3: Add the approved Fixed entries**

Insert these entries at the start of `### Fixed`:

```markdown
- `apm install` now fails before commit when declared plugin components or a
  requested `--skill` are missing, and total positional-URL failure exits `1`.
  (closes #2103, #2116, #2126; #2155)
- Failed global Claude installs now clean up bootstrap state, corrected cyclic
  dependency graphs resume without deleting `apm_modules`, and exception output
  routes through the command logger. (closes #2129, #2140, #2161; #2155)
- `apm audit --ci` now detects both changed and removed MCP declarations from
  local-path sub-packages. (closes #2127, #2136; #2155)
- Contracting the target set now reconciles `deployed_files`, removes
  APM-managed MCP servers from dropped targets, and safely adopts exact matches
  from legacy lockfiles. (closes #2139, #2149, #2158; #2155)
- Manifest and policy parsers now reject invalid identity values and unknown
  policy keys. Migration: quote numeric manifest versions and use the declared
  mapping/list types for policy blocks. (closes #2137; #2155)
- `apm compile --clean` now removes the stale context artifact when the final
  primitive is removed. (closes #2130; #2155)
- `apm uninstall` now transfers shared deployed-file ownership to a surviving
  package and persists deployment state atomically. (closes #2148, #2160; #2155)
- Semver install and update now preserve Azure DevOps bearer authentication and
  retry a stale PAT `401` with the Azure CLI bearer. (closes #2150, #2156; #2155)
```

Keep the existing entries for #2092, #2122, #2121, #2114, and #2041 after the
#2155 entries.

- [ ] **Step 4: Add the approved Performance entry**

Insert this entry before the existing #2124 performance entry:

```markdown
- Deployment-ledger reconciliation now uses indexed mutation paths, avoiding
  quadratic scans as deployment history grows. (closes #2159; #2155)
```

- [ ] **Step 5: Re-run the closure validator**

Run:

```bash
uv run python - <<'PY'
import json
import re
import subprocess
from pathlib import Path

payload = json.loads(
    subprocess.check_output(
        ["gh", "pr", "view", "2155", "--json", "closingIssuesReferences"],
        text=True,
    )
)
expected = {item["number"] for item in payload["closingIssuesReferences"]}
text = Path("CHANGELOG.md").read_text(encoding="utf-8")
release = text.split("## [0.25.0]", 1)[1].split("## [0.24.1]", 1)[0]
entries = re.findall(r"(?ms)^- .*?(?=^- |^### |^## |\Z)", release)
actual = []
for entry in entries:
    if "#2155" not in entry or "(closes " not in entry:
        continue
    closure_text = entry.split("(closes ", 1)[1].split(";", 1)[0]
    actual.extend(int(value) for value in re.findall(r"#(\d+)", closure_text))

duplicates = {value for value in actual if actual.count(value) > 1}
assert len(actual) == 24, f"expected 24 closure references, found {len(actual)}"
assert not duplicates, f"duplicate closure references: {sorted(duplicates)}"
assert set(actual) == expected, (
    f"missing={sorted(expected - set(actual))}, "
    f"extra={sorted(set(actual) - expected)}"
)
print("validated 24 unique #2155 issue closures")
PY
```

Expected:

```text
validated 24 unique #2155 issue closures
```

- [ ] **Step 6: Inspect the changelog diff**

Run:

```bash
git --no-pager diff -- CHANGELOG.md
```

Expected: 11 #2155 entries grouped under Changed, Fixed, and Performance; every
entry describes a user-visible outcome and uses `(closes ...; #2155)`.

### Task 2: Synchronize PR #2164's description

**Files:**
- Modify: `/Users/danielmeppiel/.copilot/session-state/1124f104-3167-48e8-a6cb-37c77797be67/files/pr-body-v0.25.0.md`

- [ ] **Step 1: Replace the stale WHY claim**

Replace the bullet claiming that #2155 should be one entry with:

```markdown
- [x] PR #2155 closed 24 issues across distinct user-visible surfaces.
  Compressing them into one broad entry removed the connection between
  community reports and shipped outcomes, so this release preserves grouped
  entries for each outcome family.
```

- [ ] **Step 2: Update the changelog approach and implementation rows**

Change Approach step 3 to:

```markdown
3. Rewrite `[Unreleased]` as `[0.25.0] - 2026-07-12`, keep concise entries
   for normal PRs, and preserve 11 grouped user-outcome entries for #2155 with
   all 24 closed issues linked.
```

Change the `CHANGELOG.md` implementation row to:

```markdown
| `CHANGELOG.md` | Adds the dated 0.25.0 section and restores 11 grouped #2155 outcomes covering all 24 closed issues. | Keeps an empty `[Unreleased]` placeholder and credits external contributors. |
```

- [ ] **Step 3: Update trade-offs and benefits**

Replace the stale sanitization trade-off with:

```markdown
- **Grouped outcomes instead of one entry per merged PR.** Chose 11 #2155
  entries because one unusually broad PR closed 24 distinct issues; rejected
  one mega-entry and 24 repetitive entries because neither is traceable and
  scannable.
```

Replace Benefit 2 with:

```markdown
2. Every issue closed by #2155 appears exactly once in a concrete,
   user-visible changelog outcome.
```

- [ ] **Step 4: Validate the PR body artifact**

Run:

```bash
body=/Users/danielmeppiel/.copilot/session-state/1124f104-3167-48e8-a6cb-37c77797be67/files/pr-body-v0.25.0.md
lines=$(wc -l < "$body" | tr -d ' ')
printf 'lines=%s\n' "$lines"
[ "$lines" -ge 150 ] && [ "$lines" -le 220 ]
! grep -nE '<PLACEHOLDER>|TBD|TODO' "$body"
tail -n 1 "$body" | grep -F \
  'Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>'
```

Expected: line count remains between 150 and 220, no placeholder output, and
the trailer is printed.

### Task 3: Validate, commit, push, and update the open PR

**Files:**
- Modify: `CHANGELOG.md`
- Existing plan/spec commits: `docs/superpowers/specs/2026-07-12-release-changelog-community-fixes-design.md`
  and `docs/superpowers/plans/2026-07-12-restore-release-community-fixes.md`

- [ ] **Step 1: Confirm the closure source has not changed**

Run:

```bash
uv run python - <<'PY'
import json
import re
import subprocess
from pathlib import Path

payload = json.loads(
    subprocess.check_output(
        ["gh", "pr", "view", "2155", "--json", "closingIssuesReferences"],
        text=True,
    )
)
expected = {item["number"] for item in payload["closingIssuesReferences"]}
text = Path("CHANGELOG.md").read_text(encoding="utf-8")
release = text.split("## [0.25.0]", 1)[1].split("## [0.24.1]", 1)[0]
entries = re.findall(r"(?ms)^- .*?(?=^- |^### |^## |\Z)", release)
actual = []
for entry in entries:
    if "#2155" not in entry or "(closes " not in entry:
        continue
    closure_text = entry.split("(closes ", 1)[1].split(";", 1)[0]
    actual.extend(int(value) for value in re.findall(r"#(\d+)", closure_text))

duplicates = {value for value in actual if actual.count(value) > 1}
assert len(actual) == 24, f"expected 24 closure references, found {len(actual)}"
assert not duplicates, f"duplicate closure references: {sorted(duplicates)}"
assert set(actual) == expected, (
    f"missing={sorted(expected - set(actual))}, "
    f"extra={sorted(set(actual) - expected)}"
)
print("validated 24 unique #2155 issue closures")
PY
```

Expected:

```text
validated 24 unique #2155 issue closures
```

- [ ] **Step 2: Run the release lint mirror**

Run:

```bash
./.agents/skills/cut-release/scripts/verify-lint-mirror.sh
```

Expected: four PASS rows and `[+] all lint-mirror checks PASSED`.

- [ ] **Step 3: Commit the changelog correction**

Run:

```bash
git add CHANGELOG.md
git commit -m "docs: restore v0.25.0 community fixes" \
  -m "Expand PR #2155 into grouped user outcomes and link every issue it closed." \
  -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>" \
  -m "Copilot-Session: 425fcf34-8659-4f23-a91e-a5a590208239"
```

Expected: one commit containing the changelog correction.

- [ ] **Step 4: Push the branch**

Run:

```bash
git push
```

Expected: `danielmeppiel-chore-cut-next-release` advances without force-push.

- [ ] **Step 5: Update PR #2164 through the REST-backed PR update tool**

Call `update_pull_request` with:

```text
repo_full_name: microsoft/apm
pr_number: 2164
body: Read the exact UTF-8 contents from
  /Users/danielmeppiel/.copilot/session-state/1124f104-3167-48e8-a6cb-37c77797be67/files/pr-body-v0.25.0.md
```

Expected: PR #2164 retains title `chore: release v0.25.0` and displays the
updated community-fix rationale.

- [ ] **Step 6: Verify the final PR state**

Run:

```bash
gh pr view 2164 --json state,headRefOid,body --jq '{
  state,
  headRefOid,
  hasIssueCount: (.body | contains("24 issues")),
  hasGroupedCount: (.body | contains("11 grouped"))
}'
git status --short
```

Expected: PR state is `OPEN`, both body checks are `true`, and `git status`
prints no changes.

- [ ] **Step 7: Preserve the human release gate**

Do not create or push `v0.25.0`. The post-merge tag remains the maintainer's
explicit release trigger.
