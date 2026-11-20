"""Policy fetches route through the origin-bounded HTTP owner (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.component


def test_url_credentials_become_an_explicit_basic_header(tmp_path) -> None:
    """A policy URL with user:pass is sent once, credentials moved to a header."""
    import base64

    import requests

    from apm_cli.policy.discovery import _fetch_from_url

    sent: list[requests.PreparedRequest] = []

    def _send(_adapter, request, **_kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response._content = b"name: org-policy\nversion: '1.0'\n"
        response.url = request.url
        response.request = request
        return response

    raw = "https://alice:s%40cret@policy.example.com/apm-policy.yml?sig=x"
    with patch("requests.adapters.HTTPAdapter.send", autospec=True, side_effect=_send):
        result = _fetch_from_url(raw, tmp_path, no_cache=True)

    assert result.error is None
    assert len(sent) == 1
    assert sent[0].url == "https://policy.example.com/apm-policy.yml?sig=x"
    expected = base64.b64encode(b"alice:s@cret").decode("ascii")
    assert sent[0].headers["Authorization"] == f"Basic {expected}"
