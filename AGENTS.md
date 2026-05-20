# APM Agent Instructions

This file is for AI coding agents. Read it before making any code change.

## Source Priority

1. Follow the nearest `AGENTS.md` if a future subdirectory adds one.
2. Use this file as the top-level entry point.
3. Load the matching detailed instructions when your change touches that area:
   - `.github/instructions/linting.instructions.md` for lint and formatting.
   - `.github/instructions/tests.instructions.md` for test files and test strategy.
   - `.github/instructions/encoding.instructions.md` for source, docs, and CLI output text.
   - `.github/instructions/cli.instructions.md` for CLI help and terminal output.
   - `.github/instructions/cicd.instructions.md` for workflow and release changes.
   - `.github/instructions/doc-sync.instructions.md` for user-facing docs.
   - `.github/instructions/changelog.instructions.md` for `CHANGELOG.md`.
   - `.github/instructions/integrators.instructions.md` for client/integrator work.
4. Keep this file concise. Put detailed, area-specific rules in the instruction files above.

## Setup

- Use `uv`; do not introduce another dependency manager for repo workflows.
- Install the normal development environment with:

```bash
uv sync --extra dev --extra build
uv run apm --version
```

## Before Opening A PR

- Run focused tests for the files you changed first.
- For code changes, run the CI lint mirror before pushing:

```bash
uv run --extra dev ruff check src/ tests/
uv run --extra dev ruff format --check src/ tests/
```

- For Python changes, run the relevant unit tests, then the CI unit-test command when practical:

```bash
uv run pytest tests/unit tests/test_console.py -n auto --dist worksteal
```

- Do not commit generated outputs such as coverage reports, temporary logs, build artifacts, or regenerated tool output unless the task explicitly requires them.
- Keep PRs scoped to one concern.

## Hard CI Rules

- CI standardizes on Python 3.12.
- Keep source files and CLI output printable ASCII. Do not add emoji, box drawing, curly quotes, or en/em dashes.
- Do not write YAML with direct `yaml.dump()` or `yaml.safe_dump()` calls to file handles outside `src/apm_cli/utils/yaml_io.py`; use `yaml_io.dump_yaml()`.
- Do not use `str(path.relative_to(base))`; use `portable_relpath()` from `apm_cli.utils.paths`.
- Keep new Python source files below the current CI file-length guardrail.
- Avoid duplicated code blocks; CI runs pylint `R0801` with a 10-line similarity threshold.
- For CLI output paths, use `CommandLogger` or the established Rich helper layer and `STATUS_SYMBOLS`; avoid raw `print()` in command paths.

## CLI And Docs Changes

- Keep help strings plain ASCII.
- Preserve consistent common flags such as `--verbose` / `-v`, `--dry-run`, and `--yes` / `-y`.
- When commands, flags, dependency formats, authentication flow, policy schema, or primitive formats change, update:
  - `docs/src/content/docs/`
  - `packages/apm-guide/.apm/skills/apm-usage/` when the change affects guide resources.
- Do not edit `README.md` for drift without maintainer approval; note the drift and proposed change instead.

## Tests

- Unit tests must not hit live network endpoints. Mock network calls.
- URL assertions must parse URLs with `urllib.parse.urlparse` and compare parsed components.
- Put integration tests under `tests/integration/` and gate prerequisites with module-level `pytestmark`.
- Register any new pytest marker in `pyproject.toml` and document it in `docs/src/content/docs/contributing/integration-testing.md`.

## Security And Workflows

- Do not expose secrets in source, generated files, logs, or examples.
- Validate user-controlled filesystem paths with the existing path-security helpers before reading or writing outside a trusted base.
- Do not add `pull_request` or `pull_request_target` triggers to `.github/workflows/ci-integration.yml`; it is merge-queue only and can access secrets.
- Changes under `.github/workflows/**` require lead maintainer review through CODEOWNERS.
