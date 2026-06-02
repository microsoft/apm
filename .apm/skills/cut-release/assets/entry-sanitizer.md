# entry-sanitizer -- one concise "so what" per PR

Loaded by `cut-release` at Phase 3. Rewrites the [Unreleased]
block into a dated version block with one entry per
user-facing PR.

## Per-entry rubric

For each PR that survives the DROP list below:

1. ONE entry per PR. Do not split into multiple bullets across
   sections unless the same PR genuinely changed two distinct
   user-visible surfaces (rare; usually a sign the PR was
   under-scoped).
2. TWO-SENTENCE CAP. First sentence: what changed (concrete CLI
   surface or behavior). Second sentence (optional): the "so
   what" -- what the user can now do, what bug stopped biting,
   what failure mode is closed. Drop everything else.
3. PR NUMBER at the end in parentheses: `(#1495)`. If the PR
   closes an issue, prefix: `(closes #1488, #1496)`. Multiple
   numbers go inside one parenthesis group.
4. ATTRIBUTION for external contributors: append
   `(by @username, #NNNN)`. Internal contributors are not named.
5. BREAKING marker: prefix entry with `**BREAKING:**`. Always
   land breakings in the appropriate section (usually Changed or
   Security); never bury them in Fixed.
6. CONCRETE LANGUAGE. Use the exact CLI surface affected:
   ``\`apm install -g\` now ...`` not "global install is now ...".
7. NO REPO INTERNALS. The reader is a user, not a maintainer.
   "Refactored the BFS resolver" is wrong; "`apm install --update`
   now re-resolves direct git-source semver dependencies" is right.

## Section mapping

| Diff shape | Section |
|------------|---------|
| New CLI command, new flag with new default ON, new manifest field, new spec, new policy, new feature | **Added** |
| Behavior change to existing surface (default flip, output format change, exit code change, BREAKING) | **Changed** |
| Bug fix to existing surface (was wrong, now right) | **Fixed** |
| Surface marked for removal with timeline | **Deprecated** |
| Surface removed from the CLI / config / manifest | **Removed** |
| Security-relevant fix or hardening (CVE, attack surface closed) | **Security** |
| Measurable speedup / memory reduction on a user-visible path | **Performance** |

If a PR fits two sections, pick the user-MOST-VISIBLE one and
mention the other in the body. Do not double-list.

## DROP list (do NOT include in changelog)

A PR is INTERNAL and gets dropped if ANY of these hold:

- Diff is fully contained in `.apm/` (skills, agents, instructions,
  prompts that are part of the maintainer toolkit -- the project's
  own dogfooding primitives).
- Diff is fully contained in `.github/instructions/`,
  `.github/workflows/` (unless the workflow change affects users,
  e.g. release binaries), or `.github/ISSUE_TEMPLATE/`.
- Diff is fully contained in `tests/` (test-only PR).
- Diff is fully contained in `docs/` AND the change is doc
  housekeeping (typo, restructure) rather than a doc-bug fix that
  was misleading users. Doc-bug fixes DO go in Fixed.
- Title starts with `chore(repo):`, `chore(deps):` (Dependabot-
  style), `ci:`, or `test:`.
- Title is `Merge ...` / `Revert ...` (handled separately if a
  revert is user-visible -- usually it ends up matching another
  fix entry anyway).

Phase 1's script flags these heuristically via
`is_user_facing_guess`; the rubric here is the authoritative
verdict.

EDGE CASE -- mixed-surface PRs. A PR that touches both `.apm/`
and `src/` is NOT internal -- the `src/` change is user-facing
even if the bulk of the diff is skill content. Read the `src/`
hunk to write the entry; ignore the `.apm/` noise.

## Consolidation rules

The existing [Unreleased] block may already have entries the
contributor wrote. Treat them as drafts, not gospel:

- If two existing entries point at the same PR (#NNNN appears
  twice), MERGE into one entry. Keep the longer / more specific
  prose.
- If an existing entry violates the rubric (six sentences,
  internal jargon, no "so what"), REWRITE it -- do not preserve
  the original phrasing out of politeness.
- If an existing entry references a PR not in Phase 1's output
  (likely because it was tagged Unreleased before the previous
  release), that's a bug in the previous release's sanitizer
  pass. Surface to the operator at checkpoint 2; ask whether to
  keep it (likely "fix the typo and ship") or drop it.

## Output shape

The rewritten block goes in place of `[Unreleased]`:

```
## [Unreleased]

## [X.Y.Z] - YYYY-MM-DD

### Added

- <entry>. (#NNNN)
- <entry>. (closes #AAAA, #BBBB)

### Changed

- **BREAKING:** <entry>. (#NNNN)
- <entry>. (#NNNN)

### Fixed

- <entry>. (#NNNN)
- <entry>. (by @user, #NNNN)
```

An empty `[Unreleased]` placeholder stays above the new version
heading -- the next release cycle adds entries to it.

The date is TODAY in `YYYY-MM-DD` (read from the operator's
environment / current_datetime in the session header; do not
recall).

## Worked example (v0.16.0)

Inputs from Phase 1: 19 PRs. Drop list killed 5 (skill refactor,
docs-only PRs, internal test, chore). Remaining 14:

```
### Added
- OpenAPM v0.1 normative spec ... (closes #1502, #1517)
- `ref:` on git-source dependencies now accepts semver ranges ... (closes #1488, #1496)
- `apm deps why <package>` ... (#1495)
- `policy.dependencies.require_pinned_constraint: true` ... (#1494)
- Deterministic Artifactory boundary probe ... (#1472)

### Changed
- **BREAKING:** `apm install` now exits `1` ... (#1496)
- Artifactory parse-time boundary detection ... (#1472)

### Fixed
- `install.ps1` on Windows ... (closes #1509, #1512)
- `apm.cmd` Windows shim is now written as ASCII ... (#1522)
- `install.ps1` is now strict ASCII ... (#1523)
- `apm install --target opencode` ... (Phase 1 of #581, #1513)
- `apm compile --target claude` ... (closes #1445, #1514)
- `apm install git@gitlab.com:` ... (closes #1501, #1515)
- `apm install -g` hook integration ... (closes #1499, #1516)
- `apm install --update` re-resolves git-semver ... (#1496)
- `=1.2.3` pinned classification ... (follow-up to #1494, #1506)
- `apm uninstall` windsurf cleanup ... (by @yoelabril, #1486)
- `apm unpack` deprecation banner softened ... (#1511)
```

Two PRs from Phase 1 attached to the same `#1472` -- consolidated
across Added / Changed / Fixed (the PR genuinely changed three
surfaces, so three entries is correct here, not over-listing).
