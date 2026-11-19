# entry-sanitizer -- one short line per user-visible change

Loaded by `cut-release` at Phase 3. Re-sanitizes the whole
[Unreleased] block into a dated version block where each change
has one line in one section, plus release-time Highlights.

## Per-entry rubric

The format contract is `.github/instructions/changelog.instructions.md`
(source: `.apm/instructions/changelog.instructions.md`). Read it
first, including its good/bad examples. This rubric applies it:

1. ONE CHANGE, ONE LINE, ONE SECTION. Most PRs get one line. A PR
   with several user-visible fixes groups them by theme into as
   few lines as stay clear. Only release-time Highlights may
   repeat a change.
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
5. BREAKING and must-act changes (including deprecations that
   need action) go ONLY under `### Breaking changes and upgrade
   notes`, one line each with the migration. Prefix true
   breakings with `**BREAKING:**`. Never restate them under
   Added, Changed or Deprecated.
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
| BREAKING change, or any change / deprecation users must act on | **Breaking changes and upgrade notes** (only there) |
| Behavior change to existing surface that needs no user action (output format, message wording) | **Changed** |
| Bug fix to existing surface (was wrong, now right) | **Fixed** |
| Surface marked for removal, no action needed yet | **Deprecated** |
| Surface removed from the CLI / config / manifest | **Removed** |
| Security-relevant fix or hardening (CVE, attack surface closed) | **Security** |
| Measurable speedup / memory reduction on a user-visible path | **Changed** |

If a change fits two sections, pick one: upgrade notes if users
must act, otherwise the user-MOST-VISIBLE type. Never double-list.

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

- DEDUPE ACROSS SECTIONS. If a change appears in more than one
  section (Highlights + upgrade notes + Changed is the usual
  pattern) or twice in one, keep ONE line in the right section and
  fold any caveat or migration from the others into it.
- REMOVE PR-written Highlights; they are rebuilt below.
- CONDENSE. Rewrite any line over ~25 words, any multi-sentence
  line, and any line that explains mechanism.
- GROUP. Several lines from one PR become as few themed lines as
  stay clear (for example, one line for all `apm audit` fixes);
  several PRs that deliver one change share one line.
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

Only cut-release writes `### Highlights`, and only for the
released version: 2-4 short pointers to the changes a user would
most want to hear about (new capability, broad fix, anything in
the upgrade notes), each with its PR refs. This is the one allowed
repeat of a change.

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

Fixed lines from `## [0.36.1]` in `CHANGELOG.md` show the line
style:

```
- `apm uninstall` keeps a surviving package's `skills:` selection instead of redeploying all its skills. (#3784)
- Repository names ending in `g`, `i`, `t` or `.`, such as `git/git`, install again. -- by @goranai (#3767)
- Azure DevOps installs fall back to `ADO_APM_PAT`, `az`, then your Git credential helper; not yet verified against a live organization. (closes #3752, #3753, #3758, #3790)
```

The last line condenses the 0.36.1 Azure DevOps entry to one sentence, keeping its caveat and all four refs.
