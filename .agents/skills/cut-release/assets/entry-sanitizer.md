# entry-sanitizer -- one short line per user-visible change

Loaded by `cut-release` at Phase 3. Re-sanitizes the whole
[Unreleased] block (dedupe, condense, Highlights, upgrade notes)
into a dated version block with one line per user-facing PR.

## Per-entry rubric

The format contract is `.github/instructions/changelog.instructions.md`
(source: `.apm/instructions/changelog.instructions.md`). Read it
first, including its good/bad examples. This rubric applies it:

1. ONE LINE per change. Most PRs get one line. A PR that ships
   several unrelated user-visible fixes gets one line per fix; a
   PR may also appear once in Highlights and once in the upgrade
   notes. Never two lines for the same change.
2. EFFECT FIRST, 25 WORDS OR FEWER. Say what users can now do or
   what no longer breaks. Drop mechanism, internal names, and lists
   of every touched command; the PR holds the detail.
3. PR NUMBER at the end in parentheses: `(#1495)`. If the PR
   closes an issue: `(closes #1488, #1496)`. Related PRs that
   deliver one change share one line: `(#3765, #3818)`.
4. ATTRIBUTION for external contributors, before the PR ref:
   `-- by @username (#NNNN)`. Keep every existing credit variant
   (`-- reported by`, `-- thanks`, `-- with`). Internal
   contributors are not named.
5. BREAKING and must-act changes go under `### Breaking changes
   and upgrade notes`, one line each with the migration. Prefix
   true breakings with `**BREAKING:**`. The change itself may
   also keep one short line in its type section.
6. CONCRETE LANGUAGE. Use the exact CLI surface affected:
   ``\`apm install -g\` now ...`` not "global install is now ...".
7. NO REPO INTERNALS. The reader is a user, not a maintainer.
   "Refactored the BFS resolver" is wrong; "`apm install --update`
   now re-resolves direct git-source semver dependencies" is right.
8. KEEP CAVEATS that change what a user should trust (for example
   "Not yet verified against a live Azure DevOps organization").

## Section mapping

| Diff shape | Section |
|------------|---------|
| New CLI command, new flag with new default ON, new manifest field, new spec, new policy, new feature | **Added** |
| Behavior change to existing surface (default flip, output format change, exit code change, BREAKING) | **Changed** |
| Bug fix to existing surface (was wrong, now right) | **Fixed** |
| Surface marked for removal with timeline | **Deprecated** |
| Surface removed from the CLI / config / manifest | **Removed** |
| Security-relevant fix or hardening (CVE, attack surface closed) | **Security** |
| Measurable speedup / memory reduction on a user-visible path | **Changed** |

If a PR fits two sections, pick the user-MOST-VISIBLE one. Do
not double-list (the upgrade-notes exception in rule 5 aside).

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

The existing [Unreleased] block was written PR by PR and drifts.
Treat every line as a draft and re-sanitize the whole block:

- DEDUPE. If two lines describe the same change (typically the
  same PR number, often one short and one long version), MERGE
  them into one short line; keep the user-visible effect and any
  caveat, drop the rest.
- CONDENSE. Rewrite any line over ~25 words, any multi-sentence
  line, and any line that explains mechanism. Move must-act
  details (migration steps, newly blocked cases) into the upgrade
  notes as one line.
- GROUP. Several lines from one PR that describe small fixes to
  the same surface may become one line; several PRs that deliver
  one change share one line with all their numbers.
- DROP internal-only lines (test, CI, maintainer tooling) per the
  DROP list, even if a contributor added them.
- PRESERVE every user-facing PR reference and every credit. After
  rewriting, diff the set of `#NNNN` refs and `@user` credits
  before vs after; only DROP-list PRs may disappear.
- If an existing entry references a PR not in Phase 1's output
  (likely because it was tagged Unreleased before the previous
  release), surface it to the operator at checkpoint 2; ask
  whether to keep it or drop it.

## Highlights

Write `### Highlights` last: 2-4 lines naming the changes a user
would most want to hear about (new capability, broad fix, anything
in the upgrade notes). Each is one short line with its PR refs.

## Output shape

The rewritten block goes in place of `[Unreleased]`:

```
## [Unreleased]

## [X.Y.Z] - YYYY-MM-DD

### Highlights

- <headline change>. (#NNNN)

### Breaking changes and upgrade notes

- **BREAKING:** <change>; <one-line migration>. (#NNNN)
- <must-act note>; <what to do>. (#NNNN)

### Added

- <entry>. (#NNNN)
- <entry>. -- by @user (#NNNN)

### Changed

- <entry>. (closes #AAAA, #BBBB)

### Fixed

- <entry>. (#NNNN)
```

Omit empty sections. Section order: Highlights, Breaking changes
and upgrade notes, Added, Changed, Deprecated, Removed, Fixed,
Security.

An empty `[Unreleased]` placeholder stays above the new version
heading -- the next release cycle adds entries to it.

The date is TODAY in `YYYY-MM-DD` (read from the operator's
environment / current_datetime in the session header; do not
recall).

## Worked example (v0.36.1)

`## [0.36.1]` in `CHANGELOG.md` is the reference shape: three
Highlights, one upgrade note with its migration, one Changed line,
and Fixed lines such as:

```
- `apm uninstall` keeps a surviving package's `skills:` selection instead of redeploying all its skills. (#3784)
- Repository names ending in `g`, `i`, `t` or `.`, such as `git/git`, install again. -- by @goranai (#3767)
- Azure DevOps installs, ... use one chain: `ADO_APM_PAT`, `az`, then your Git credential helper; `az` errors show the real reason. Not yet verified against a live Azure DevOps organization. (closes #3752, #3753, #3758, #3790)
```

The last line keeps its caveat and groups four refs into one line.
