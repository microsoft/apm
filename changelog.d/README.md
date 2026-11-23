# CHANGELOG fragments

A pull request with a user-visible effect adds one file here instead of
editing `## [Unreleased]` in `CHANGELOG.md`. Each PR writes its own file,
so concurrent PRs never conflict. `cut-release` assembles the fragments into
the version block and deletes them.

## File name

`<pr>.<section>.md`, where `<section>` is one of:

| Slug | CHANGELOG section |
|------|-------------------|
| `breaking-changes-and-upgrade-notes` | Breaking changes and upgrade notes |
| `added` | Added |
| `changed` | Changed |
| `deprecated` | Deprecated |
| `removed` | Removed |
| `fixed` | Fixed |
| `security` | Security |

There is no Highlights fragment: `cut-release` writes Highlights.

## Content

Exactly one bullet line, ASCII only, in the wording rules of
`.github/instructions/changelog.instructions.md`, ending with the PR number.
For example, `changelog.d/3784.fixed.md` holds:

```
- `apm uninstall` keeps a surviving package's `skills:` selection instead of redeploying all its skills. (#3784)
```

`(closes #N, #PR)`, several PR numbers, and `-- by @user (#PR)` credits are
fine, as long as the file's PR number comes last.

## Commands

```bash
python .apm/skills/cut-release/scripts/changelog_fragments.py check
python .apm/skills/cut-release/scripts/changelog_fragments.py render
```

`check` validates every fragment. `render` prints the release block that the
fragments, plus any legacy `[Unreleased]` bullets, produce.
