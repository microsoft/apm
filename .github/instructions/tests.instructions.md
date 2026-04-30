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
