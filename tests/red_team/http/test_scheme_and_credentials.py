"""Scheme abuse and embedded-credential handling at the executor layer.

Two concerns:

* Scheme allowlist (``d3-scheme``): the executor must dispatch ONLY for
  ``https://``. ``http``, ``file``, ``gopher``, ``ftp`` and scheme-less
  URLs must never reach ``requests.post``. These are regression traps --
  they PASS on head (defense exists) and would catch a future
  loosening.

* Embedded credentials (``d3-cred-log``): a ``https://user:pass@host``
  URL must never write the cleartext credential to the scripts log. The
  executor redacts the URL before logging (``_redact_url_credentials``).
  We assert the log line contains no credential material. Note (residual,
  not a break): the executor still passes the credential-bearing URL to
  ``requests.post`` -- embedded creds are blocked at the ``validate``
  layer, not re-stripped here; that is documented as defense-in-depth.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from apm_cli.core import script_executors
from apm_cli.core.script_executors import _redact_url_credentials

from .conftest import make_event, make_http_script

REJECTED_SCHEMES = [
    ("plain-http", "http://insecure.example.com/hook"),
    ("file", "file:///etc/passwd"),
    ("gopher", "gopher://169.254.169.254/_GET"),
    ("ftp", "ftp://files.example.com/x"),
    ("schemeless", "example.com/hook"),
    ("ws", "ws://example.com/socket"),
    ("data", "data:text/plain,hello"),
]


class TestSchemeAllowlist:
    @pytest.mark.parametrize(
        "url",
        [s[1] for s in REJECTED_SCHEMES],
        ids=[s[0] for s in REJECTED_SCHEMES],
    )
    def test_non_https_scheme_never_dispatched(self, dispatch, url: str) -> None:
        recorder = dispatch(make_http_script(url))
        assert not recorder.dispatched, (
            f"non-https scheme {urlparse(url).scheme!r} must not be dispatched"
        )


class TestEmbeddedCredentialRedaction:
    def test_redaction_helper_strips_userinfo(self) -> None:
        cleaned = _redact_url_credentials("https://alice:s3cret@hooks.example.com/p")
        parsed = urlparse(cleaned)
        assert parsed.username is None
        assert parsed.password is None
        assert parsed.hostname == "hooks.example.com"
        # No userinfo separator survives in the parsed authority component.
        assert "@" not in (parsed.netloc or "")

    def test_credentials_never_reach_scripts_log(self, dispatch, monkeypatch, tmp_path) -> None:
        log_path = tmp_path / "scripts.log"
        monkeypatch.setattr(script_executors, "_get_scripts_log_path", lambda: log_path)
        url = "https://carol:hunter2@webhook.internal-team.example/notify"
        recorder = dispatch(make_http_script(url), make_event())
        assert recorder.dispatched  # the (redacted) request still goes out

        contents = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        assert "hunter2" not in contents
        assert "carol" not in contents
        # The logged target, if present, must be the credential-free host.
        url_tokens = [tok for tok in contents.split() if "://" in tok]
        for tok in url_tokens:
            parsed = urlparse(tok.strip(" \t\r\n()[]"))
            assert parsed.password is None
            assert parsed.username is None

    def test_redaction_keeps_port(self) -> None:
        cleaned = _redact_url_credentials("https://u:p@host.example.com:8443/x")
        parsed = urlparse(cleaned)
        assert parsed.hostname == "host.example.com"
        assert parsed.port == 8443
        assert parsed.username is None
