---
name: cut-release
description: >-
  Use this skill to cut an APM release from the current worktree:
  assess whether the cycle since the last tag warrants a patch or
  minor bump (semver discipline against the merged-since-last-tag
  diff), sanitize the [Unreleased] CHANGELOG block into a dated
  version block with one concise "so what" entry per merged PR
  (drop internal-only churn, consolidate duplicates), bump
  pyproject.toml + uv.lock, run the CI-mirror lint chain, and open
  the release PR. Activate on "ship a release", "cut v0.x",
  "release prep", "bump and PR", "open release PR", "what kind of
  release do we need", or any phrasing that ends in opening a
  release PR -- even when the user does not say "skill". Stops
  BEFORE tagging; tagging stays a human gate that triggers the
  release workflow. Refuses to bump to a major (>= 1.0.0) version
  without explicit operator confirmation.
---

# cut-release -- assess, sanitize, bump, lint, open PR

This skill drives the release-cut workflow end-to-end on the current
worktree. It STOPS at "PR open"; tagging is the human-gated trigger
that fires the release workflow.

## When to use

Trigger this skill on any of these intents:

- "cut a release"
- "ship v0.x" / "ship the release"
- "release prep" / "prepare the release"
- "bump and open the PR"
- "open the release PR"
- "what kind of release do we need now"
- "we have enough merged for a release"
- Any phrasing that ends in opening a release PR.

Do NOT trigger on:

- "tag v0.16.0" -- tagging is the post-merge step the operator runs.
- "publish a blog post" / "draft release notes for the website".
- "what changed since v0.15.0" -- enumeration only, no release.
- "regenerate uv.lock" / raw version-string edits.
- "rollback to v0.15.0" -- release reversal is a different skill.

## Companion primitives

The skill DEPENDS on these existing primitives in the same source
tree. Do not duplicate their content; reference them.

- `.apm/skills/apm-strategy/SKILL.md` -- versioning judgement and
  breaking-change lens. Auto-loads via its own trigger on
  `CHANGELOG.md` and release-pipeline edits. Treat it as the
  authoritative lens for "is this BREAKING" and "does this
  positioning change warrant a migration note".
- `.apm/instructions/linting.instructions.md` -- canonical
  CI-mirror lint chain. Reference; do not re-derive ruff / pylint
  command lists in this skill.
- `.github/instructions/changelog.instructions.md` -- Keep-a-
  Changelog format contract. Reference; do not redefine the
  format.

## Output charset rule

Per `.github/instructions/encoding.instructions.md`, source files in
this bundle (SKILL.md, assets, scripts) MUST stay in printable ASCII.
The PR body and changelog entries this skill produces are also
written to repo files (CHANGELOG.md, PR body), so they MUST stay
ASCII too. Use `--` for em dashes, `[!]` / `[+]` / `[x]` for status
markers, and so on.

## Procedure

Run these phases in order. Reload `plan.md` (the session memento)
at the start of each phase and after every tool return.

### Phase 0 -- ground state

1. Read the current branch with `git rev-parse --abbrev-ref HEAD`.
   The skill assumes you are on a release-prep branch (or are
   willing to create one). If you are on `main`, STOP and ask the
   operator to put you on a branch.
2. Resolve the last release tag with `git describe --tags --abbrev=0`.
3. Persist the goal to plan.md:
   - Range: `<last-tag>..HEAD`.
   - Acceptance: release PR is open, lint mirror green, no version
     bumped beyond what the cycle warrants.

### Phase 1 -- enumerate merged PRs

Run `scripts/list-changes-since-tag.sh` (no arguments). It emits
JSON on stdout: one object per merged PR with fields `pr`, `title`,
`labels`, `author`, `paths_summary`, `is_user_facing_guess`.

The script's heuristic for `is_user_facing_guess` is
intentionally conservative -- it flags as INTERNAL only when the
diff is fully contained in `.apm/`, `.github/instructions/`,
`tests/`, `docs/`, or matches `chore(repo)`. Treat the field as a
HINT, not a verdict; the LLM makes the final call per
`assets/entry-sanitizer.md`.

DO NOT rely on training-recalled PR titles. Truth #5: pretraining
is frozen. Every PR title in the changelog must come from the
script's stdout.

### Phase 2 -- pick the bump (B10 CHECKPOINT 1)

Load `assets/semver-rubric.md`. Walk every PR from Phase 1
against the rubric. The rubric outputs PATCH, MINOR, or
ESCALATE-TO-MAJOR.

Show the operator:

- The chosen bump and the next version number.
- Per-PR rationale (which signal fired).
- The "why patch/minor" one-liner that will go into the PR body.

If the rubric outputs ESCALATE-TO-MAJOR (>= 1.0.0), STOP and
surface the escalation per `assets/semver-rubric.md` "Major bump"
section. Do not bump to 1.0.0 without explicit operator
confirmation.

Wait for the operator's confirm before continuing.

### Phase 3 -- sanitize the changelog (B10 CHECKPOINT 2)

Load `assets/entry-sanitizer.md`. For each PR classified as
user-facing in Phase 1:

1. Choose its section: Added (new feature, new command, new spec),
   Changed (behavior change in existing surface), Fixed (bug fix),
   Deprecated, Removed, Security, Performance.
2. Write ONE entry per PR. Two-sentence cap. "So what" framing.
   PR number in parentheses at the end. Preserve `by @author` for
   external contributors.
3. Consolidate any pre-existing Unreleased entries that point at
   the same PR.
4. Drop INTERNAL PRs entirely (the script flagged most of them;
   the rubric in entry-sanitizer.md confirms).
5. Mark BREAKING entries with `**BREAKING:**` prefix.

Rewrite the CHANGELOG.md `[Unreleased]` block in place:

```
## [Unreleased]

## [X.Y.Z] - YYYY-MM-DD

### Added
...
### Changed
...
### Fixed
...
```

Keep an empty `[Unreleased]` placeholder above the new version
heading. The date is today (read from the operator's environment,
not recall).

Show the operator the unified diff against CHANGELOG.md. Wait for
confirm before continuing.

### Phase 4 -- bump version files

Run `scripts/bump-version.sh <new-version>`. The script edits
`pyproject.toml` (the `version = "..."` line in `[project]`) and
runs `uv lock` to refresh `uv.lock`. It prints a unified diff to
stdout for human review.

If the script exits non-zero, surface the error and STOP -- do not
continue with a partial bump.

### Phase 5 -- verify lint mirror (S4 gate; B10 CHECKPOINT 3 on fail)

Run `scripts/verify-lint-mirror.sh`. It mirrors the four CI lint
steps from `.apm/instructions/linting.instructions.md` verbatim
(ruff check, ruff format --check, pylint R0801, auth-signals).

If it passes (exit 0), proceed to Phase 6.

If it fails (exit 1), STOP. Surface the failures to the operator.
Common cures:

- ruff diagnostics -- run `uv run --extra dev ruff check src/
  tests/ --fix` and `uv run --extra dev ruff format src/ tests/`.
- pylint R0801 -- duplication landed via a recent main commit;
  merge main first, then re-run.
- auth-signals -- consult `scripts/lint-auth-signals.sh` output.

Do NOT push or open the PR until lint is green. The PR description
will claim CI is green; that claim must hold.

### Phase 6 -- commit, push, open PR

1. `git add CHANGELOG.md pyproject.toml uv.lock`.
2. `git commit -m "chore: release vX.Y.Z" -m "<short body>"`.
   The body mirrors prior release commits (#1410, #1454, #1526):
   one paragraph naming what was bumped and the lint-mirror
   confirmation, plus the "post-merge: tag vX.Y.Z" reminder.
   Include the standard `Co-authored-by` trailer.
3. `git push -u origin HEAD`.
4. Compose the PR body from `assets/pr-body-template.md`,
   substituting `{version}` (unprefixed, e.g. `0.16.1` -- used by
   `CHANGELOG.md` and `pyproject.toml`), `{tag}` (v-prefixed, e.g.
   `v0.16.1` -- used by the post-merge `git tag` block, since the
   release workflow's stable-release regex requires the `v` prefix
   per `.github/workflows/build-release.yml`), `{date}`,
   `{bump_rationale}`, and (if BREAKING) `{breaking_summary}`.
5. `gh pr create --base main --head <branch> --title
   "chore: release vX.Y.Z" --body-file <tmpfile>`.
6. Echo the PR URL.
7. Surface the post-merge step explicitly: "Post-merge: tag
   `vX.Y.Z` to trigger the release workflow." Do NOT tag.

### Phase 7 -- update plan and exit

Mark each plan.md todo as done. The session ends here. Tagging is
the operator's decision and lives outside this skill.

## Anti-patterns this skill refuses

- TAGGING. Tagging is a separate, human-gated step. If asked to
  tag inside this session, decline and remind the operator that
  the release workflow expects `git tag vX.Y.Z && git push --tags`
  after PR merge.
- BUMP-TO-MAJOR-UNPROMPTED. 0.x -> 1.0.0 is a positioning
  decision. ESCALATE per `assets/semver-rubric.md`.
- HAND-ROLLED PR TITLES. Every changelog entry must be backed by
  a PR title from `scripts/list-changes-since-tag.sh` stdout. No
  recall.
- CHATTY GATE. Three operator checkpoints, no more (version pick,
  sanitized changelog, lint-fail recovery). Do not ask per-PR or
  per-section.
- PARTIAL PUSH. If lint fails, do not push. The release PR
  asserts green CI; pushing red breaks the contract.
- SKIPPED LINT. Do not skip Phase 5 even if the diff looks
  trivial. The lint mirror catches code-duplication drift on the
  merge commit, which is invisible to a local diff inspection.

## Boundary

This skill does NOT:

- Tag the release.
- Run the test suite or build PyInstaller binaries.
- Publish a GitHub Release body, blog post, or announcement.
- Decide whether a release should happen -- the operator decided
  that by activating this skill.
- Bump to >= 1.0.0 without explicit operator confirmation.
- Review code quality or test coverage (use `apm-review-panel`
  before activating this skill if a recent PR needs review).
