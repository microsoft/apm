"""Redirect suppression and timeout enforcement (regression traps).

``d3-redirect`` / ``d3-timeout``. These assert defenses that already
exist on head, so they PASS now and guard against regressions:

* ``allow_redirects=False`` is always passed -- a 30x to an internal host
  is therefore never auto-followed (the SSRF-via-redirect pivot is shut).
* A timeout is always passed to ``requests.post`` and defaults to 10s for
  http scripts (``effective_timeout``). It is passed as a ``(connect, read)``
  tuple, and the dispatcher additionally enforces a TOTAL wall-clock deadline
  (round-21) so a slow-loris dribble -- which resets a scalar per-recv timeout
  on every byte -- cannot hold the worker past it.

The attacker-influenced ``timeoutSec`` is clamped to a finite ceiling
(``_MAX_HTTP_TIMEOUT``): a zero / negative / non-finite value falls back to the
ceiling and a huge value is capped, so a malicious project apm.yml cannot set a
multi-day timeout.
"""

from __future__ import annotations

import pytest

from apm_cli.core.lifecycle_scripts import ScriptEntry
from apm_cli.core.script_executors import _HTTP_CONNECT_TIMEOUT, _MAX_HTTP_TIMEOUT

from .conftest import make_http_script


def _read_deadline(timeout: object) -> float:
    """Extract the read element from a scalar-or-tuple requests timeout."""
    return timeout[1] if isinstance(timeout, tuple) else timeout


class TestRedirectSuppression:
    def test_allow_redirects_is_false(self, dispatch) -> None:
        recorder = dispatch(make_http_script("https://hooks.example.com/p"))
        assert recorder.dispatched
        assert recorder.last.allow_redirects is False

    def test_redirect_kwarg_present_explicitly(self, dispatch) -> None:
        """The kwarg must be explicit, not relying on requests' default."""
        recorder = dispatch(make_http_script("https://hooks.example.com/p"))
        assert "allow_redirects" in recorder.last.kwargs


class TestTimeoutEnforcement:
    def test_timeout_passed_to_post(self, dispatch) -> None:
        recorder = dispatch(make_http_script("https://hooks.example.com/p"))
        assert recorder.dispatched
        assert recorder.last.timeout is not None
        assert "timeout" in recorder.last.kwargs

    def test_default_http_timeout_is_ten(self, dispatch) -> None:
        recorder = dispatch(make_http_script("https://hooks.example.com/p"))
        assert _read_deadline(recorder.last.timeout) == 10

    def test_effective_timeout_default_for_http(self) -> None:
        script = ScriptEntry(script_type="http", event="post-install", url="https://x.example")
        assert script.effective_timeout == 10

    def test_configured_timeout_is_honoured(self, dispatch) -> None:
        recorder = dispatch(make_http_script("https://hooks.example.com/p", timeout_sec=3))
        assert _read_deadline(recorder.last.timeout) == 3

    @pytest.mark.parametrize("value", [0, -1, 100000])
    def test_unusual_timeout_is_clamped(self, dispatch, value: int) -> None:
        """Round-21: an attacker-influenced timeout is clamped to a finite ceiling.

        Zero / negative / non-finite -> the ceiling; a huge value -> capped. The
        old contract (passed through verbatim) was the slow-loris bug.
        """
        recorder = dispatch(make_http_script("https://hooks.example.com/p", timeout_sec=value))
        timeout = recorder.last.timeout
        assert _read_deadline(timeout) == _MAX_HTTP_TIMEOUT
        connect = timeout[0] if isinstance(timeout, tuple) else timeout
        assert connect <= _HTTP_CONNECT_TIMEOUT
