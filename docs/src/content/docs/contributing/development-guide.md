---
title: "Development Guide"
description: "How to contribute to APM — setup, coding style, testing, and pull request process."
sidebar:
  order: 1
---

Thank you for considering contributing to APM! This document outlines the process for contributing to the project.

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](https://github.com/microsoft/apm/blob/main/CODE_OF_CONDUCT.md). Please read it before contributing.

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
4. Run tests: `uv run pytest`
5. Ensure your code passes linting: `uv run --extra dev ruff check src/ tests/`
6. Commit your changes with a descriptive message.
7. Push to your fork.
8. Submit a pull request.

### Pull Request Process

1. Fill out the PR template — describe what changed, why, and link the issue.
2. Ensure your PR addresses only one concern (one feature, one bug fix).
3. Include tests for new functionality.
4. Update documentation if needed.
5. PRs must pass all CI checks before they can be merged.

### Issue Triage

Every new issue is automatically labeled `needs-triage`. Maintainers review incoming issues and:

1. **Accept** — remove `needs-triage`, add `accepted`, and assign a milestone.
2. **Prioritize** — optionally add `priority/high` or `priority/low`.
3. **Close** — if it's a duplicate (`duplicate`) or out of scope, close with a comment explaining why.

Labels used for triage: `needs-triage`, `accepted`, `needs-design`, `priority/high`, `priority/low`.

## Development Environment

This project uses uv to manage Python environments and dependencies:

```bash
# Clone the repository
git clone https://github.com/microsoft/apm.git
cd apm

# Install all dependencies (creates .venv automatically)
uv sync --extra dev
```

## Testing

We use pytest for testing. After completing the setup above, run the test suite with:

```bash
uv run pytest -q
```

If you don't have `uv` available, you can use a standard Python venv and pip:

```bash
# create and activate a venv (POSIX / WSL)
python -m venv .venv
source .venv/bin/activate

# install this package in editable mode and test deps
pip install -U pip
pip install -e .[dev]

# run tests
pytest -q
```

### Running the bounded mutation pilot

The advisory mutation pilot covers five stable owners: dependency subset
selection, update-plan construction, cached-policy serialization, canonical
in-package link projection, and lockfile field normalization (the fail-closed
`host_type`/`exec_status` normalizers, not the `@dataclass` reconstruction
methods `to_dict`/`from_dict`/`to_dependency_ref` -- mutmut cannot mutate
`@dataclass` methods; those are defended by PR #2246's manual mutation-break
twins instead). It runs nightly or by manual workflow
dispatch, not as required PR CI, and has a 20-minute hosted job budget.

Run the exact-function allowlist locally:

```bash
uv run --frozen --extra dev python scripts/run_mutation_pilot.py \
  --output mutation-pilot-report.json
```

The command fails on new survivors, timeouts, suspicious results, unchecked
mutants, and incomplete outcomes. Its timestamp-free JSON report is suitable
for comparing runs. Pass `--reuse-cache` only when the allowlisted source,
tests, configuration, runner, and lockfile are unchanged.

To inspect existing mutmut metadata without executing mutants:

```bash
uv run --frozen --extra dev python scripts/run_mutation_pilot.py \
  --report-only --output mutation-pilot-report.json
```

The reviewed survivor allowlist lives in
[`tests/mutation/baseline.json`](https://github.com/microsoft/apm/blob/main/tests/mutation/baseline.json).
Do not update it to make a run green. Inspect surviving diffs with `mutmut show`
and add behavioral tests for real contract gaps. Use `--update-baseline` only
when the baseline change itself has been reviewed:

```bash
uv run --frozen --extra dev python scripts/run_mutation_pilot.py \
  --update-baseline --output mutation-pilot-report.json
```

The repository
[`CONTRIBUTING.md`](https://github.com/microsoft/apm/blob/main/CONTRIBUTING.md#running-the-bounded-mutation-pilot)
owns the review policy for baseline changes.

## Coding Style

This project follows:
- [PEP 8](https://pep8.org/) for Python style guidelines
- We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting

CI enforces all lint and formatting rules automatically. You can run them locally:

```bash
uv run --extra dev ruff check src/ tests/         # lint
uv run --extra dev ruff check --fix src/ tests/   # lint with auto-fix
uv run --extra dev ruff format src/ tests/        # format
```

Verify Ruff lint and format before pushing:

```bash
uv run --extra dev ruff check src/ tests/ && uv run --extra dev ruff format --check src/ tests/
```

CI also runs repository guardrails from `.github/workflows/ci.yml`, including duplication and auth-boundary checks.

### Optional: local pre-commit hooks

For instant feedback before pushing, install the pre-commit hooks:

```bash
uv run pre-commit install
```

This is optional -- CI is the authoritative gate. The pre-commit hook rev may lag behind the CI version; check `.pre-commit-config.yaml` against `uv.lock` if you see discrepancies.

## Documentation

If your changes affect how users interact with the project, update the documentation accordingly.
Public top-level CLI commands and rendered reference pages are a matched contract. When you add,
remove, or rename a command, create, remove, or rename its matching page under
`docs/src/content/docs/reference/cli/` and update the command table in
`docs/src/content/docs/reference/index.md`, then run:

```bash
npm --prefix docs run build
uv run --frozen python scripts/check_cli_docs.py docs/dist
```

## License

By contributing to this project, you agree that your contributions will be licensed under the project's [MIT License](https://github.com/microsoft/apm/blob/main/LICENSE).

## Questions?

If you have any questions, feel free to open an issue or reach out to the maintainers.
