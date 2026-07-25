---
applyTo: "tests/**"
description: "Test conventions: URL assertions must use urllib.parse, never substring."
---

# Test Conventions

## URL assertions: use `urllib.parse`, never substring

Any assertion that a URL appears in or matches some output **must** parse the
URL with `urllib.parse.urlparse` and compare on a parsed component
(`hostname`, `port`, `scheme`, `path`). Substring assertions like
`assert "host.example.com" in msg` or `assert "https://x" in url` are flagged
by CodeQL as `py/incomplete-url-substring-sanitization` (high severity, "the
string may be at an arbitrary position in the URL") and **will fail CI**.

This rule applies regardless of whether the value being asserted looks like a
"safe" hostname — CodeQL is a static check and cannot infer that `host` in
`assert host in msg` is bounded; the alert fires anyway.

### Wrong

```python
# Substring match -- CodeQL py/incomplete-url-substring-sanitization
assert "registry.example.com" in msg
assert "https://api.github.com/v0/servers" in url
assert "127.0.0.1" in warning_text

# Set membership of substring -- still flagged (CodeQL can't infer set type)
hosts = {urlparse(tok).hostname for tok in msg.split() if "://" in tok}
assert "poisoned.example.com" in hosts
```

### Right

```python
from urllib.parse import urlparse

# Direct hostname equality on a parsed URL token
urls = [tok for tok in msg.split() if "://" in tok]
assert len(urls) == 1
assert urlparse(urls[0]).hostname == "registry.example.com"

# Set equality (not membership) when multiple URLs are expected
hosts = {urlparse(tok.strip("()")).hostname for tok in msg.split() if "://" in tok}
assert hosts == {"a.example.com", "b.example.com"}

# Component-level checks for path / scheme / port
parsed = urlparse(url)
assert parsed.scheme == "https"
assert parsed.hostname == "api.github.com"
assert parsed.path == "/v0/servers"
```

### Helper pattern for multi-URL output

When asserting against logger / CLI output that may contain multiple URLs,
extract them with a small helper and assert on the parsed tuple:

```python
def _printed_urls(text: str) -> list[tuple[str, str, str]]:
    """Extract (scheme, hostname, path) tuples from any URLs in text."""
    from urllib.parse import urlparse
    out = []
    for token in text.split():
        cleaned = token.strip("(),.;'\"")
        if "://" not in cleaned:
            continue
        p = urlparse(cleaned)
        out.append((p.scheme, p.hostname or "", p.path))
    return out

assert ("https", "registry.example.com", "/v0/servers") in _printed_urls(msg)
```

`tests/unit/test_mcp_command.py` already uses this pattern; reuse it (or
copy it) rather than inventing a new substring check.

## Why the rule applies even to "obviously safe" tests

The CodeQL rule is intentionally conservative: a substring assertion against a
URL string is the same code shape as a security-critical sanitizer check, and
the analyzer cannot tell them apart. Treating every URL assertion uniformly
through `urlparse` keeps CI green AND reinforces the security pattern that
production code must follow (see
`src/apm_cli/install/mcp/registry.py::_redact_url_credentials` and
`src/apm_cli/install/mcp/registry.py::_is_local_or_metadata_host`).

## Other rules

- **No live network calls.** Tests must never hit a real HTTP endpoint; use
  `unittest.mock.patch('requests.Session.get')` or
  `monkeypatch.setattr(client.session, "get", fake)`. Live-inference tests
  are isolated to `ci-runtime.yml` and gated by `APM_RUN_INFERENCE_TESTS=1`.

- **Patch where the name is looked up.** When a function moved to
  `apm_cli/install/phases/X.py` is still patched by tests at
  `apm_cli.commands.install.X`, the patch silently no-ops. Either patch at
  the new canonical path, or use module-attribute access in the call site
  (`X_mod.function`) so canonical patches survive the move. See
  `src/apm_cli/install/phases/integrate.py:888` for the pattern.

- **Reuse existing fixtures.** Common fixtures live in `tests/conftest.py`
  and `tests/unit/install/conftest.py`. Don't re-implement temp-dir or
  mock-logger fixtures inline.

- **Targeted runs during iteration.** Run the specific test file first
  (`uv run pytest tests/unit/install/test_X.py -x`) before running the
  full suite (`uv run pytest tests/unit tests/test_console.py`).

## Integration tests: placement and markers

The integration suite uses **declarative gating** via pytest markers,
not per-file orchestrator enumeration. Adding a new integration test
does not require editing `scripts/test-integration.sh`.

### Three independent marker axes

Markers compose across three axes:

| Axis | Question | Markers |
|---|---|---|
| Behavioral | What boundary does the test cross? | `unit`, `component`, `e2e` |
| Scheduling | When is the test selected? | `integration`, `slow`, `benchmark`, `live` |
| Prerequisite | What environment must exist? | `requires_*` |

`live` is both an opt-in scheduling marker and an external-service
prerequisite. Behavioral markers do not replace prerequisite markers.

### Procedure

1. Drop the file under `tests/integration/test_<feature>.py`.
2. At the top of the module, declare any scheduling and prerequisite
   markers as a single `pytestmark`:

   ```python
   import pytest

   pytestmark = pytest.mark.requires_network_integration
   # OR for multiple prerequisites:
   pytestmark = [
       pytest.mark.requires_e2e_mode,
       pytest.mark.requires_runtime_codex,
   ]
   ```

That is it. The orchestrator (`scripts/test-integration.sh`) and the
CI integration job collect everything under `tests/integration/` in
a single `pytest` invocation; markers are honored automatically.

### Prerequisite selection

Declare every prerequisite the test needs. The full registry lives in
`pyproject.toml` under
`[tool.pytest.ini_options].markers` and is documented (with the
opt-in commands) in
[`docs/src/content/docs/contributing/integration-testing.md`](../../docs/src/content/docs/contributing/integration-testing.md).
Quick map for the common cases:

| Test prerequisite                            | Marker                          |
|----------------------------------------------|---------------------------------|
| Real HTTP to APM-owned services              | `requires_network_integration`  |
| Real codex / copilot / llm runtime binary    | `requires_runtime_<name>`       |
| Downloads runtimes; full E2E flow            | `requires_e2e_mode`             |
| GitHub / ADO token required                  | `requires_github_token` / `requires_ado_pat` |
| Paid or third-party external service         | `live` (deselected by default)  |
| Performance measurement                      | `benchmark` (deselected by default) |
| Hermetic (mocks all I/O)                     | *no marker required*            |

Need a marker that does not exist yet? Register it in
`pyproject.toml` AND add a row to the docs registry table in the
same PR. Both must stay in sync.

### Behavioral classification for the critical suite

| Marker | Definition |
|---|---|
| `unit` | Pure logic with no filesystem and no CLI |
| `component` | In-process behavior that touches a filesystem or one command boundary |
| `e2e` | A real installed CLI crossing at least one command boundary |

`pyproject.toml` owns these definitions.
`tests/quality/critical_suite.toml` owns the finite classified module set.
Directory names and filename suffixes are not behavioral evidence. For
example, `test_policy_pinned_constraint_e2e.py` is `component` because it uses
Click in-process; `test_core_smoke.py` is `e2e` because it invokes an installed
binary through subprocess boundaries.

To extend the finite manifest:

1. Confirm every test in the module crosses the same behavioral boundary.
2. Add the literal module path and marker to `critical_suite.toml`.
3. Add the module-level behavioral `pytestmark`; preserve independent
   scheduling and prerequisite markers.
4. If the filename suggests a different boundary, document why behavior wins.
5. Run the taxonomy and quality contracts:

```bash
uv run --extra dev pytest -p no:cacheprovider -q tests/quality
uv run --frozen python scripts/check_test_assertions.py
uv run --frozen python scripts/check_exact_test_duplicates.py
```

Baseline updates may only tighten reductions. A new duplicate or assertion
violation must be fixed, not accepted by an updater:

```bash
uv run --frozen python scripts/check_test_assertions.py --update-baseline
uv run --frozen python scripts/check_exact_test_duplicates.py --update-baseline
```

Provisional mode is CI-only and permitted only while a pull request is a draft.
Contributor commands, ready pull requests, merge queue runs, and final
validation are strict and reject `provisional` metadata. Do not pass the
internal provisional flag manually; remove the metadata after remeasurement
and review.

### Anti-patterns (will land as `recommended` findings on review)

- **Editing `scripts/test-integration.sh` per file.** The orchestrator
  enumerates the directory, not the files. Per-file blocks are drift
  by construction.
- **Runtime self-skips inside the test body.** A bare
  `if not os.getenv("APM_E2E_TESTS"): pytest.skip(...)` runs before
  collection-time gating and weakens the contract. Use
  module-level `pytestmark` instead -- declarative gating is the
  single source of truth.
- **Reading the gate env var inside test logic.** If your test
  reads `APM_RUN_INTEGRATION_TESTS` to branch behaviour, the marker
  is wrong (or missing). The marker is the gate; the test body
  should assume the gate already passed.

## The `windows_compat` marker (cross-platform regression contract)

`windows_compat` is a **scheduling marker**, not a prerequisite
marker. Do not confuse it with `requires_windows` -- there is no
`requires_windows` marker in this repo, and there must never be one
used for this purpose. `windows_compat` tests run on **every OS**
(Linux, macOS, Windows) as part of the normal unit suite; the marker
additionally selects them for a dedicated, focused PR-time job
(`windows-compat-gate` in `.github/workflows/ci.yml`, `runs-on:
windows-latest`) that runs `pytest -m windows_compat tests/unit`.
That job exists because the full Windows matrix in
`build-release.yml` only runs post-merge -- without this gate, a
Windows-only regression (CRLF line endings, backslash path
separators, bare `git` argv resolution, socket/thread shutdown races,
etc.) is structurally invisible until after the PR has already
merged. See microsoft/apm#2233 for the incident that motivated it.

### When to add the marker

Add `pytest.mark.windows_compat` to a test when it is a **load-bearing
regression proof for a cross-platform defect class** -- i.e. it would
have caught a bug that only manifests on Windows (or that a
non-portable implementation could silently reintroduce), such as:

- Text/JSON writers that must produce LF-only, atomic, ASCII-safe
  output regardless of platform line-ending or encoding defaults.
- Path formatting that must stay POSIX-style in diagnostics/output
  even when the underlying OS uses backslash separators.
- Subprocess invocation that must resolve an executable name (e.g.
  `git`) portably instead of assuming a POSIX `$PATH` lookup.
- Socket/thread lifecycle code whose shutdown path differs by OS
  (e.g. Windows-specific WinError codes needing a narrow, proven
  catch -- never a broad exception swallow).

Do **not** add the marker to a whole file just because it happens to
live near Windows-relevant code -- mark only the specific tests that
assert the cross-platform contract, unless (per module) virtually
every test in the file already exercises that same code path (in
which case a module-level `pytestmark = pytest.mark.windows_compat`
is the right level of granularity -- see
`tests/unit/test_shepherd_owner_touch_gate.py` for an example where
nearly every test goes through the same git-executable-resolution
helper).

### Procedure

1. Register the marker once in `pyproject.toml` under
   `[tool.pytest.ini_options].markers` (already done -- do not
   re-register). `--strict-markers` is set, so an unregistered marker
   name fails collection immediately; this is deliberate and must not
   be worked around with a bare string literal.
2. Apply the marker at the narrowest level that is true:
   ```python
   @pytest.mark.windows_compat
   def test_write_report_is_deterministic_atomic_and_printable_ascii():
       ...
   ```
   or, for a module dedicated to the contract family:
   ```python
   pytestmark = pytest.mark.windows_compat
   ```
3. Do **not** edit `.github/workflows/ci.yml` to add your test file --
   the gate selects by marker, not by file enumeration. If your test
   is properly marked, it is picked up automatically the next PR run.
4. Confirm collection locally:
   ```bash
   uv run --extra dev pytest -m windows_compat tests/unit --collect-only -q
   ```
5. `tests/unit/test_windows_compat_gate_workflow.py` asserts the
   *shape* of the gate (marker-scoped, bounded, non-empty, required,
   non-duplicative of the Linux full suite) -- it does not enumerate
   file names, so it does not need editing when you add or remove a
   marked test.

### Anti-patterns

- **Enumerating test files in the workflow instead of using the
  marker.** This is the exact drift the marker-based gate replaces;
  a file list silently goes stale as tests move or get renamed.
- **Using `requires_windows` (or inventing it) for this purpose.**
  That name implies "only runs on Windows" -- the opposite of what
  this marker means. These tests are cross-platform regression
  proofs that run everywhere.
- **Marking a whole large, multi-purpose test file** when only a
  handful of its tests actually assert the Windows-regression
  contract -- this bloats the PR-time gate with unrelated coverage
  that belongs to the ordinary Linux unit run instead.
