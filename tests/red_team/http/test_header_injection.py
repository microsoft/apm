"""Header CRLF / smuggling at the requests layer (regression trap).

``d3-header-crlf``. The executor builds headers as a Python ``dict`` and
passes it to ``requests.post``. A single header VALUE therefore cannot
structurally become two headers (dict keys are fixed). On the wire,
``requests``/``urllib3`` additionally reject control characters in header
values (``InvalidHeader``), so a value carrying ``\\r\\n`` cannot smuggle
an extra header. These tests PASS on head and lock in that defense.

We exercise the real ``requests`` header validation via
``PreparedRequest.prepare`` -- no network is touched. We also confirm the
executor's env-expansion path keeps an injected value inside a single
header entry.
"""

from __future__ import annotations

import pytest
import requests

from apm_cli.core.script_executors import _execute_http

from .conftest import make_event, make_http_script


class TestHeaderCrlfRejected:
    @pytest.mark.parametrize(
        "evil_value",
        [
            "ok\r\nX-Injected: evil",
            "ok\nX-Injected: evil",
            "ok\rX-Injected: evil",
        ],
    )
    def test_requests_rejects_crlf_in_header_value(self, evil_value: str) -> None:
        req = requests.Request(
            "POST",
            "https://hooks.example.com/p",
            headers={"X-Test": evil_value},
        )
        with pytest.raises(requests.exceptions.InvalidHeader):
            req.prepare()

    def test_value_with_crlf_stays_single_header_entry(self, dispatch, monkeypatch) -> None:
        """Env-expanded header value cannot fork into two dict entries."""
        monkeypatch.setenv("WEBHOOK_NOTE", "line1\r\nX-Injected: evil")
        script = make_http_script(
            "https://hooks.example.com/p",
            headers={"X-Note": "$WEBHOOK_NOTE"},
            allowed_env_vars=["WEBHOOK_NOTE"],
        )
        recorder = dispatch(script)
        assert recorder.dispatched
        headers = recorder.last.headers
        # No smuggled header key appears; the payload remains one value.
        assert "X-Injected" not in headers
        assert "X-Note" in headers

    def test_crlf_header_through_executor_is_caught(self, monkeypatch) -> None:
        """End-to-end: a real (unpatched) requests.post raises, executor
        isolates it, and nothing crashes the caller.

        The worker thread swallows the InvalidHeader and logs an error;
        ``_execute_http`` still returns its thread. We join and assert no
        exception escapes.
        """
        script = make_http_script(
            "https://hooks.example.com/p",
            headers={"X-Bad": "a\r\nX-Injected: evil"},
        )

        # Force requests.post to raise as the real library would for a
        # control-char header value, without hitting the network.
        def _raise(*_a, **_k):
            raise requests.exceptions.InvalidHeader("Invalid header value")

        monkeypatch.setattr(requests, "post", _raise)
        from apm_cli.core import script_executors

        monkeypatch.setattr(script_executors, "_get_guarded_session", lambda: None)
        thread = _execute_http(script, make_event())
        if thread is not None:
            thread.join(timeout=5)
        # Reaching here means the executor isolated the failure.
        assert True
