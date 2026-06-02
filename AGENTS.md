# AGENTS.md

This file is for AI coding agents. Read it before making any code change.

It documents CI-enforced rules and project conventions so agents can comply
on the first attempt instead of learning from failed runs. Most sections
correspond to a CI check; conventions enforced in review are marked as
"Convention; not CI-gated" so you know exactly what is and is not a hard
gate.

Human contributor? See [CONTRIBUTING.md](CONTRIBUTING.md) for the full
contribution workflow.

---

## 1. Setup

Install all dependencies (including dev and build extras):

```bash
uv sync --extra dev --extra build
```

Verify the CLI works:

```bash
uv run apm --version
```

Python version: `pyproject.toml` requires **>=3.10**. CI runners use Python
**3.12**. Write code compatible with >=3.10; do not use 3.12-only syntax.
Use **uv only** for dependency management -- never invoke `pip` directly.

---

## 2. Before Every Commit

Run this sequence and fix every failure before committing:

```bash
# 1. Auto-fix style and import order
uv run --extra dev ruff check src/ tests/ --fix
uv run --extra dev ruff format src/ tests/

# 2. Verify lint (must be silent)
uv run --extra dev ruff check src/ tests/
uv run --extra dev ruff format --check src/ tests/

# 3. Code duplication guardrail (pylint R0801)
uv run --extra dev python -m pylint \
  --disable=all --enable=R0801 \
  --min-similarity-lines=10 \
  --fail-on=R0801 \
  src/apm_cli/

# 4. Auth-protocol boundary check
bash scripts/lint-auth-signals.sh

# 5. Run unit tests with coverage gate
uv run pytest tests/unit tests/test_console.py \
  -n auto --dist worksteal \
  --cov --cov-report term-missing --cov-fail-under=60 -q
```

CI also enforces two grep-based rules that ruff cannot catch -- check them
manually if you touched the relevant surfaces:

```bash
# YAML I/O safety: no yaml.dump() to file handles outside yaml_io.py
grep -rn --include='*.py' -P 'yaml\.(safe_)?dump\(.+,\s*[a-zA-Z_]\w*\b' \
  src/apm_cli/ | grep -v 'utils/yaml_io.py' | grep -v '# yaml-io-exempt'

# Path portability: no raw str(path.relative_to(...))
grep -rn --include="*.py" -P 'str\([^)]*\.relative_to\(' \
  src/apm_cli/ | grep -v portable_relpath | grep -v '.pyc'
```

Both must produce **no output**. If they do, see the fix in sections 3.2
and 3.3 below.

---

## 3. Code Conventions

### 3.1 General

- Target Python **>=3.10** (CI runners use 3.12); use modern syntax (`list` / `dict` / `X | None`
  instead of `List` / `Dict` / `Optional[X]`).
- Use **type hints** on all function parameters and return values.
  (Convention; not CI-gated.)
- Write docstrings on all public functions and classes.
  (Convention; not CI-gated.)
- Keep every `src/**/*.py` file under **2450 lines**. The current worst
  case is ~2404 lines; tighten over time.
- No raw `print()` calls in production code -- use `CommandLogger` or the
  Rich console helpers in `src/apm_cli/utils/console.py`.
  (Convention; T201 is not in the ruff select list, but reviewers will flag
  unguarded print calls.)

### 3.2 YAML I/O rule

All YAML writes to file handles must go through `yaml_io.dump_yaml()` in
`src/apm_cli/utils/yaml_io.py`. Raw `yaml.dump(data, file_handle)` and
`yaml.safe_dump(data, file_handle)` are blocked by CI lint.

```python
# Wrong
with open(path, "w") as fh:
    yaml.dump(data, fh)

# Right
from apm_cli.utils.yaml_io import dump_yaml
dump_yaml(data, path)
```

If a direct call is genuinely unavoidable, add `# yaml-io-exempt` on that
line and document why in a code comment.

### 3.3 Path portability rule

Never use `str(path.relative_to(base))`. On Windows the resulting string
uses backslashes and breaks cross-platform logic.

```python
# Wrong
rel = str(some_path.relative_to(root))

# Right
from apm_cli.utils.paths import portable_relpath
rel = portable_relpath(some_path, root)
```

### 3.4 Output and logging

- Use `CommandLogger` from `src/apm_cli/core/command_logger.py` for
  structured, verb-controlled output.
- Use ASCII status bracket symbols defined in `STATUS_SYMBOLS` (in
  `src/apm_cli/utils/console.py`) for all CLI-facing messages:

  | Symbol | Meaning              |
  |--------|----------------------|
  | `[+]`  | success / confirmed  |
  | `[!]`  | warning              |
  | `[x]`  | error                |
  | `[i]`  | info                 |
  | `[*]`  | action / processing  |
  | `[>]`  | running / progress   |
  | `[~]`  | update / refreshed   |
  | `[-]`  | removed              |
  | `[=]`  | unchanged / equal    |
  | `[#]`  | list / metrics       |

- All source code and CLI output must stay within **printable ASCII**
  (U+0020-U+007E). No emojis, no Unicode box-drawing, no curly quotes,
  no em/en dashes.

### 3.5 Code duplication (pylint R0801)

Blocks of 10 or more identical lines across two files trigger the
duplication guardrail. Extract shared logic into a base class or helper
module instead of duplicating.

### 3.6 Auth-protocol boundary

`get_bearer_provider` must only be imported inside the auth boundary
(`core/auth.py`, `core/azure_cli.py`) or tests. All other callers must
route through `AuthResolver.execute_with_bearer_fallback`. See
`scripts/lint-auth-signals.sh` for the full rule set and exemption
mechanism.

---

## 4. CLI Conventions

- Help strings must be **plain ASCII** -- no emojis.
- Help text format: `help="Verb-phrase description in sentence case."`
- Standard flag names: `--verbose` / `-v`, `--dry-run`, `--yes` / `-y`,
  `--output` / `-o`.
- No `TODO` or placeholder text in help strings.
- Every **new** command or flag must be documented in the matching file
  under `docs/src/content/docs/reference/cli/` in the **same PR**.
- Every **changed** command or flag must have its docs entry updated in
  the same PR.
- The CLI Consistency Checker workflow (`.github/workflows/cli-consistency-checker.md`)
  runs weekly and files issues for any drift between CLI help text and the
  reference docs in `docs/src/content/docs/reference/cli/`.

---

## 5. Testing Conventions

### 5.1 Placement

- Hermetic / offline tests: `tests/unit/`
- End-to-end / network tests: `tests/integration/`
- The CI unit job runs with sharding and a 60% coverage floor:
  `uv run pytest tests/unit tests/test_console.py --splits 2 --group N -n 2 --dist worksteal --cov --cov-fail-under=60`

### 5.2 Markers

Use module-level `pytestmark` for integration test gating:

```python
import pytest
pytestmark = pytest.mark.requires_network_integration
```

Full marker registry is in `pyproject.toml` under
`[tool.pytest.ini_options].markers`. Never read gate env vars inside test
bodies -- the marker is the gate.

### 5.3 Mocking

- Mock **all** network calls in unit tests.
  Use `unittest.mock.patch` or `monkeypatch.setattr`.
- Patch at the canonical import path, not at the call-site alias.
- Reuse fixtures from `tests/conftest.py` and the per-domain conftest
  files (`tests/unit/commands/conftest.py`,
  `tests/unit/marketplace/conftest.py`) before writing new ones.

### 5.4 URL assertions

URL assertions must use `urllib.parse`, never substring matching.
CodeQL flags `assert "host.example.com" in msg` as a security issue.

```python
# Wrong
assert "registry.example.com" in msg

# Right
from urllib.parse import urlparse
parsed = urlparse(url_token)
assert parsed.hostname == "registry.example.com"
```

### 5.5 Coverage

- Per-shard floor (PR time): **60%**
- Global floor (merge gate): **80%** (enforced in `pyproject.toml` and
  `ci-integration.yml`)

### 5.6 Constraints

- Do **not** add new external test dependencies without a tracked issue.
- Do **not** commit generated files (coverage data, `.pyc`, build
  artifacts).
- Do **not** edit `scripts/test-*.sh` in the same PR as a feature change.

---

## 6. Security

- **CodeQL** (`codeql.yml`) runs Python + Actions static analysis on every
  PR and weekly. Fix any finding before requesting review.
- **No secrets in source**. Never hard-code tokens, passwords, or API
  keys.
- **Path traversal**: validate and sanitise all user-supplied paths before
  use. Use `src/apm_cli/utils/path_security.py` for safe path validation.
  Additional helpers live in `src/apm_cli/integration/base_integrator.py`
  and `src/apm_cli/utils/paths.py`.
- **Subprocess**: the ruff `S603`/`S607` rules are suppressed for
  `src/apm_cli/**` because subprocess calls are intentional in a CLI
  tool -- but every subprocess invocation must still pass only controlled,
  validated arguments.

---

## 7. PR Hygiene

- Branch names follow `<user>/<issue-number>-<short-slug>` convention.
- Every PR that changes code, tests, docs, or dependencies needs a
  `CHANGELOG.md` entry under `## [Unreleased]` (see
  `.github/instructions/changelog.instructions.md`).
- Integration tests (`ci-integration.yml`) run only on the merge queue
  tentative merge commit, not on every PR push. Fork PRs without write
  access never reach the integration workflow.
- Do **not** modify `scripts/test-*.sh` in the same PR as feature changes.
- If your change affects a CLI command or flag, update the matching file
  under `docs/src/content/docs/reference/cli/` and the matching file in
  `packages/apm-guide/.apm/skills/apm-usage/` in the same PR.
- **Do not edit `README.md`** without explicit maintainer approval -- raise
  the drift in the PR body instead.

---

## 8. Keeping This File Up to Date

When any CI rule, lint check, or workflow convention changes, update this
file in the same PR. This file is the single source of truth for agents;
stale rules cost CI minutes and human review time.
