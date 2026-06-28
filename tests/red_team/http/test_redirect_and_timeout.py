"""Redirect suppression and timeout enforcement (regression traps).

``d3-redirect`` / ``d3-timeout``. These assert defenses that already
exist on head, so they PASS now and guard against regressions:

* ``allow_redirects=False`` is always passed -- a 30x to an internal host
  is therefore never auto-followed (the SSRF-via-redirect pivot is shut).
* A timeout is always passed to ``requests.post`` and defaults to 10s for
  http scripts (``effective_timeout``), bounding hang/slowloris exposure.

Negative / zero / huge configured timeouts are passed through verbatim;
that is a configuration concern (the value comes from a trusted script
file), documented as a non-break here.
"""

from __future__ import annotations

import pytest

from apm_cli.core.lifecycle_scripts import ScriptEntry

from .conftest import make_http_script


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
        assert recorder.last.timeout == 10

    def test_effective_timeout_default_for_http(self) -> None:
        script = ScriptEntry(script_type="http", event="post-install", url="https://x.example")
        assert script.effective_timeout == 10

    def test_configured_timeout_is_honoured(self, dispatch) -> None:
        recorder = dispatch(make_http_script("https://hooks.example.com/p", timeout_sec=3))
        assert recorder.last.timeout == 3

    @pytest.mark.parametrize("value", [0, -1, 100000])
    def test_unusual_timeout_passed_through(self, dispatch, value: int) -> None:
        """Non-break: trusted script-file value flows through unchanged."""
        recorder = dispatch(make_http_script("https://hooks.example.com/p", timeout_sec=value))
        assert recorder.last.timeout == value
