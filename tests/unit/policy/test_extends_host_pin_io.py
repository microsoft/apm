"""Security Finding F1: extends: host pinning + redirect refusal (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.component


def _assert_redirect_target_host(error: str, expected_host: str) -> None:
    """Extract the redirect *target* URL from *error* and compare hostname.

    Production format: ``Refusing HTTP redirect (NNN) from <src> to <dst>``.
    We parse the destination URL and compare ``urlparse(...).hostname``
    so CodeQL's ``py/incomplete-url-substring-sanitization`` rule is
    satisfied.
    """
    match = re.search(r"\bto\s+(https?://\S+)", error)
    assert match is not None, f"no redirect target URL in error: {error!r}"
    parsed = urlparse(match.group(1).rstrip(").,;"))
    assert parsed.hostname == expected_host


from apm_cli.policy.discovery import (  # noqa: E402
    _fetch_from_url,
)

# ----------------------------------------------------------------------
# Host-pin enforcement on extends: chain walk
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Redirect refusal in _fetch_from_url
# ----------------------------------------------------------------------


class TestFetchFromUrlRedirectRefusal:
    """_fetch_from_url must NOT follow HTTP redirects (SSRF / Referer leak)."""

    @patch("apm_cli.utils.http_credentials.CredentialScopedSession.request")
    def test_fetch_from_url_disables_redirects(self, mock_request):
        """A 301 response is returned as fetch failure, not silently followed."""
        mock_resp = MagicMock()
        mock_resp.status_code = 301
        mock_resp.headers = {"Location": "https://attacker.example.com/leak"}
        mock_request.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_url(
                "https://example.com/policy.yml",
                Path(tmpdir),
                no_cache=True,
            )

        # One hop, sent without automatic redirect following; never followed.
        assert mock_request.call_count == 1
        assert mock_request.call_args.kwargs.get("allow_redirects") is False

        # Result is a fetch failure with a clear error message.
        assert result.policy is None
        assert result.outcome == "cache_miss_fetch_fail"
        assert "redirect" in (result.error or "").lower()
        _assert_redirect_target_host(result.error or "", "attacker.example.com")

    @patch("apm_cli.utils.http_credentials.CredentialScopedSession.request")
    def test_fetch_from_url_302_also_refused(self, mock_request):
        """Any 3xx redirect class is refused, not just 301."""
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://example.com/elsewhere/policy.yml"}
        mock_request.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_url(
                "https://example.com/policy.yml",
                Path(tmpdir),
                no_cache=True,
            )

        # A same-origin hop to another path is refused too: never followed.
        assert mock_request.call_count == 1
        assert result.policy is None
        assert "redirect" in (result.error or "").lower()
