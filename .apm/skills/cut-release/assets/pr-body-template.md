Cut release {version}.

- Bump `pyproject.toml` to {version} (and `uv.lock`).
- Move `[Unreleased]` to `[{version}] - {date}` in `CHANGELOG.md`, re-sanitized to one short line per user-facing PR merged since {prev_version} (deduped, with Highlights and upgrade notes).

## Why {bump_kind} ({version}), not {alt_bump_kind} ({alt_version})

{bump_rationale}

{breaking_summary_block}

## Validation

Lint mirror green locally (ruff check + format, pylint R0801, auth-signals). See `.apm/instructions/linting.instructions.md` for the contract this mirrors.

## Post-merge

Tag `{tag}` to trigger the release workflow:

```
git tag {tag}
git push origin {tag}
```
