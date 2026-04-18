# AGENTS.md

This file is for AI coding agents. Read it before making any code change in this repository.

For detailed per-module rules, see the instruction files in `.github/instructions/`.

## Setup

```
uv sync --extra dev
uv run apm --version
```

- Use `uv` for all dependency management. Never use `pip` directly.
- Python 3.12 (CI-enforced via `ci.yml`).

## Before Every Commit

Run these checks before every commit. All must pass.

1. **Unit tests**
   ```
   uv run pytest tests/unit tests/test_console.py -n auto --dist worksteal
   ```

2. **YAML I/O lint** -- must return empty (no output = pass)
   ```
   grep -rn --include='*.py' -P 'yaml\.(safe_)?dump\(.+,\s*[a-zA-Z_]\w*\b' src/apm_cli/ | grep -v 'utils/yaml_io.py' | grep -v '# yaml-io-exempt'
   ```

3. **Path portability lint** -- must return empty (no output = pass)
   ```
   grep -rn --include='*.py' -P 'str\([^)]*\.relative_to\(' src/apm_cli/ | grep -v portable_relpath
   ```

4. **Encoding check** -- verify no non-ASCII characters in changed text files
   ```
   git diff --cached --name-only --diff-filter=ACMR | xargs grep -Pn '[^\x09\x0a\x0d\x20-\x7e]'
   ```

5. **CHANGELOG entry** -- add an entry under `[Unreleased]` for any PR changing code, tests, or docs.

## Code Conventions

| Rule | Detail |
|------|--------|
| Python version | 3.12 (CI-enforced) |
| Package manager | `uv` only -- never `pip` |
| Formatter | `black` (line-length 88) |
| Import sorting | `isort` (profile=black) |
| YAML I/O | Use `yaml_io.load_yaml()` / `yaml_io.dump_yaml()` from `apm_cli.utils.yaml_io`. Never write `yaml.dump(data, file_handle)` directly. CI grep enforces this. Exempt with `# yaml-io-exempt` comment. |
| Path portability | Use `portable_relpath(path, base)` from `apm_cli.utils.paths`. Never use `str(path.relative_to(base))`. CI grep enforces this. |
| Path security | Validate user-supplied paths with `validate_path_segments()` and `ensure_path_within()` from `apm_cli.utils.path_security`. Never build filesystem paths from user input without these guards. |
| Console output | Use `_rich_success()`, `_rich_error()`, `_rich_warning()`, `_rich_info()` from `apm_cli.utils.console`. No raw `print()` calls. Rich library with colorama fallback. |
| Encoding | ALL source code and CLI output must stay within printable ASCII (U+0020--U+007E). No emojis, no Unicode symbols. Use bracket notation for status: `[+]` success, `[!]` warning, `[x]` error, `[i]` info, `[*]` action, `[>]` running. These map to `STATUS_SYMBOLS` in `console.py`. |

## CLI Conventions

- Help text must be plain ASCII (no emojis).
- Format: `help="Action description"` (sentence case, no period).
- Use Rich library for visual output with colorama fallback.
- Every new or modified CLI command/flag must be reflected in `docs/src/content/docs/reference/cli-commands.md`.
- Verify examples in documentation actually work.

## Testing Conventions

| Directory | Purpose |
|-----------|---------|
| `tests/unit/` | Fast isolated unit tests (default CI scope) |
| `tests/integration/` | E2E tests requiring network/external services |
| `tests/test_console.py` | Root-level test included in CI suite |

- Mock all network calls in unit tests.
- No new external test dependencies without maintainer approval.
- No generated files committed to the repository.
- CI command: `uv run pytest tests/unit tests/test_console.py -n auto --dist worksteal`

## Security

- CodeQL runs on every PR (Python + GitHub Actions analysis).
- Never commit secrets, tokens, or credentials to source.
- Path traversal validation: use `path_security.py` guards for all user-supplied paths.
- See `apm_cli/utils/path_security.py` for `validate_path_segments()`, `ensure_path_within()`, `safe_rmtree()`.

## PR Hygiene

- Branch naming: `fix/NNN-short-description` or `feat/NNN-short-description`.
- Every PR changing code, tests, or docs must include a CHANGELOG.md entry under `[Unreleased]`.
- Do NOT modify `scripts/test-*.sh` files in feature PRs (integration tests run from `main` branch only).
- Keep PRs focused -- one logical change per PR.

## Detailed Per-Module Rules

All files are in `.github/instructions/`.

| File | Scope | Description |
|------|-------|-------------|
| `encoding.instructions.md` | `**` | Cross-platform ASCII encoding rules |
| `doc-sync.instructions.md` | `**` | Documentation sync rules |
| `cli.instructions.md` | `src/apm_cli/cli.py` | CLI design guidelines and visual standards |
| `integrators.instructions.md` | `src/apm_cli/integration/**` | BaseIntegrator architecture pattern |
| `changelog.instructions.md` | `CHANGELOG.md` | Changelog format (Keep a Changelog) |
| `cicd.instructions.md` | `.github/workflows/**` | CI/CD pipeline architecture |
