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
is two steps.

### Procedure

1. Drop the file under `tests/integration/test_<feature>.py`.
2. At the top of the module, declare the runtime / network / E2E
   prerequisites as a single `pytestmark`:

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

### Marker selection

Pick the marker that matches the **strongest** prerequisite the test
has. The full registry lives in `pyproject.toml` under
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
