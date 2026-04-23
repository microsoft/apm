# Contributing to APM

Thank you for considering contributing to APM! This document outlines the process for contributing to the project.

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). Please read it before contributing.

## How to Contribute

### Reporting Bugs

Before submitting a bug report:

1. Check the [GitHub Issues](https://github.com/microsoft/apm/issues) to see if the bug has already been reported.
2. Update your copy of the code to the latest version to ensure the issue hasn't been fixed.

When submitting a bug report:

1. Use our bug report template.
2. Include detailed steps to reproduce the bug.
3. Describe the expected behavior and what actually happened.
4. Include any relevant logs or error messages.

### Suggesting Enhancements

Enhancement suggestions are welcome! Please:

1. Use our feature request template.
2. Clearly describe the enhancement and its benefits.
3. Provide examples of how the enhancement would work.

### Development Process

1. Fork the repository.
2. Create a new branch for your feature/fix: `git checkout -b feature/your-feature-name` or `git checkout -b fix/issue-description`.
3. Make your changes.
4. Run tests: `uv run pytest tests/unit tests/test_console.py -x`
5. Ensure your code follows our coding style (we use Black and isort).
6. Commit your changes with a descriptive message.
7. Push to your fork.
8. Submit a pull request.

### Pull Request Process

1. Fill out the PR template - describe what changed, why, and link the issue.
2. Ensure your PR addresses only one concern (one feature, one bug fix).
3. Include tests for new functionality.
4. Update documentation if needed.
5. PRs must pass all CI checks before they can be merged.

### How merging works

This repo uses GitHub's native **merge queue**. Once your PR is approved, a
maintainer adds it to the queue. The queue then:

1. Builds a tentative merge of your PR against the latest `main` - no manual
   "Update branch" needed.
2. Runs the integration suite against that tentative merge.
3. Auto-merges if checks pass; ejects from the queue if they fail.

What this means for contributors:

- You don't need to keep your branch up to date with `main` manually.
- The fast unit + build checks (Tier 1) run on every push to your PR.
- The full integration suite (Tier 2) only runs once your PR is in the queue,
  not on every WIP push.

If your PR is ejected from the queue because of a real failure, push a fix and
ask a maintainer to re-queue.

### Issue Triage

Every new issue is automatically labeled `needs-triage`. Maintainers review incoming issues and:

1. **Accept** - remove `needs-triage`, add `accepted`, and assign a milestone.
2. **Prioritize** - optionally add `priority/high` or `priority/low`.
3. **Close** - if it's a duplicate (`duplicate`) or out of scope, close with a comment explaining why.

Labels used for triage: `needs-triage`, `accepted`, `needs-design`, `priority/high`, `priority/low`.

## Development Environment

This project uses uv to manage Python environments and dependencies:

```bash
# Clone the repository
git clone <this-repo-url>
cd apm

# Install all dependencies (creates .venv automatically)
uv sync --extra dev
```

## Testing

We use pytest for testing with `pytest-xdist` for parallel execution. After completing the setup above:

```bash
# Run the unit test suite (recommended - matches CI, fast)
uv run pytest tests/unit tests/test_console.py -x

# Run a specific test file (fastest, use during development)
uv run pytest tests/unit/path/to/relevant_test.py -x

# Run the full test suite (includes integration & acceptance tests)
uv run pytest

# Run with verbose output
uv run pytest tests/unit -x -v
```

Tests run in parallel automatically (`-n auto` is configured in `pyproject.toml`). To force serial execution, add `-n0`.

If you don't have `uv` available, you can use a standard Python venv and pip:

```bash
# create and activate a venv (POSIX / WSL)
python -m venv .venv
source .venv/bin/activate

# install this package in editable mode and test deps
pip install -U pip
pip install -e .[dev]

# run unit tests
pytest tests/unit tests/test_console.py -x
```

## Coding Style

This project follows:
- [PEP 8](https://pep8.org/) for Python style guidelines
- We use Black for code formatting and isort for import sorting

You can run these tools with:

```bash
uv run black .
uv run isort .
```

## Documentation

If your changes affect how users interact with the project, update the documentation accordingly.

## Extending APM

### How to add an experimental feature flag

Use an experimental flag to de-risk rollout of a user-visible behavioural change that may need early adopter feedback. Do not add a flag for a bug fix, internal refactor, or any change that should simply ship as the default behaviour.

Experimental flags MUST NOT gate security-critical behaviour (content scanning, path validation, lockfile integrity, token handling, MCP trust, collision detection). Flags are ergonomic/UX toggles only.

When adding a new experimental flag:

1. Register it in `src/apm_cli/core/experimental.py` in the `FLAGS` dict with a frozen `ExperimentalFlag(name=..., description=..., default=False, hint=...)`.
2. Gate the code path with a function-scope import (avoids import cycles):
   ```python
   def my_function():
       from apm_cli.core.experimental import is_enabled
       if is_enabled("my_flag"):
           ...
   ```
3. Add tests that cover both the enabled and disabled code paths.
4. Update the experimental command reference page at `docs/src/content/docs/reference/experimental.md`.

Naming rules:

- Use `snake_case` in the registry and config.
- Use `kebab-case` for display and other user-facing strings.
- The CLI accepts both forms on input.

Graduation and retirement:

1. When a flag becomes the default, remove the gate and remove the matching `FLAGS` entry in the same PR.
2. Add a `CHANGELOG.md` entry under `Changed` with a migration note if the previous default differed.

Avoid these anti-patterns:

- Do not gate security-critical behaviour behind an experimental flag.
- Do not read `is_enabled()` at module import time.
- Do not persist flag state anywhere other than `~/.apm/config.json` via `update_config`.

## License

By contributing to this project, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).

## Questions?

If you have any questions, feel free to open an issue or reach out to the maintainers.

Thank you for your contributions!
