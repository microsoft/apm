"""Unit coverage for the ``_fetch_ado`` marketplace fetcher (REST items API).

Coverage parity with the GitLab REST fetcher (``test_marketplace_client``):
- ADO single-file reads hit ``/_apis/git/repositories/.../items`` (no clone)
- ``ADO_APM_PAT`` -> HTTP Basic ``base64(":" + PAT)`` (auth-first)
- AAD bearer context -> ``Authorization: Bearer <jwt>``
- confirmed 404 -> ``None`` (no wasted clone; path auto-detection probes next)
- sign-in HTML / network failure -> generic-git fallback (no regression)
- non-decomposable ADO URL -> generic-git directly
- token is never embedded in the request URL
- ``_FETCHERS`` dispatch routes ``kind == "ado"`` to ``_fetch_ado``
"""

from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.marketplace import client as client_mod
from apm_cli.marketplace.client import _ado_auth_header, _fetch_ado
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import MarketplaceSource

_ADO_URL = "https://dev.azure.com/contoso/platform/_git/tools"
_MANIFEST = {"name": "acme", "plugins": []}


def _ado_source(url: str = _ADO_URL, name: str = "acme", ref: str = "main") -> MarketplaceSource:
    return MarketplaceSource(name=name, url=url, ref=ref)


def _ado_host_info() -> SimpleNamespace:
    return SimpleNamespace(host="dev.azure.com", kind="ado")


def _assert_ado_items_url(
    url: str,
    *,
    hostname: str,
    path: str,
    file_path: str = "marketplace.json",
    ref: str = "main",
) -> None:
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.hostname == hostname
    assert parsed.path == path
    query = parse_qs(parsed.query)
    assert query["path"] == [file_path]
    assert query["versionDescriptor.version"] == [ref]
    assert query["api-version"] == ["7.0"]


def _fake_response(status_code: int, *, text: str = "", content_type: str = "application/json"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"Content-Type": content_type}

    def _raise_for_status():
        if status_code >= 400:
            raise client_mod.requests.exceptions.HTTPError(f"HTTP {status_code}")

    resp.raise_for_status.side_effect = _raise_for_status
    # The capped API reader streams the body via ``iter_content`` (stream=True);
    # yield the encoded text so the fake matches the real streamed contract.
    resp.iter_content.side_effect = lambda chunk_size=65536: iter(
        [text.encode("utf-8")] if text else []
    )
    resp.close.side_effect = lambda: None
    return resp


class _FakeResolver:
    """Single-attempt stand-in for ``AuthResolver.try_with_fallback``.

    Records routing kwargs and invokes the operation with a controllable
    ``(token, git_env)`` pair so the fetcher's header derivation and dispatch
    can be exercised without real credential resolution.
    """

    def __init__(self, token: str | None = None, git_env: dict | None = None):
        self.token = token
        self.git_env = git_env or {"GIT_TERMINAL_PROMPT": "0"}
        self.calls: list[dict] = []

    def try_with_fallback(self, host, operation, *, org=None, path=None, unauth_first=False):
        self.calls.append({"host": host, "org": org, "path": path, "unauth_first": unauth_first})
        return operation(self.token, self.git_env)


# ---------------------------------------------------------------------------
# _ado_auth_header
# ---------------------------------------------------------------------------


def test_ado_auth_header_none_token_is_anonymous() -> None:
    assert _ado_auth_header(None, {}) == {}


def test_ado_auth_header_pat_uses_basic() -> None:
    headers = _ado_auth_header("pat-123", {"GIT_TOKEN": "pat-123"})
    expected = base64.b64encode(b":pat-123").decode("ascii")
    assert headers == {"Authorization": f"Basic {expected}"}


def test_ado_auth_header_bearer_detected_from_git_env() -> None:
    git_env = {"GIT_CONFIG_VALUE_0": "Authorization: Bearer jwt-xyz"}
    assert _ado_auth_header("jwt-xyz", git_env) == {"Authorization": "Bearer jwt-xyz"}


# ---------------------------------------------------------------------------
# _fetch_ado -- REST fast path
# ---------------------------------------------------------------------------


def test_fetch_ado_uses_rest_items_api_not_clone() -> None:
    captured: list[tuple[str, dict]] = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        captured.append((url, dict(headers or {})))
        return _fake_response(200, text=json.dumps(_MANIFEST))

    resolver = _FakeResolver(token="pat-abc", git_env={"GIT_TOKEN": "pat-abc"})
    with patch("apm_cli.marketplace.client._http_get", side_effect=fake_get):
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=_ado_host_info(),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    assert len(captured) == 1
    url, headers = captured[0]
    _assert_ado_items_url(
        url,
        hostname="dev.azure.com",
        path="/contoso/platform/_apis/git/repositories/tools/items",
    )
    # PAT routed as HTTP Basic, never embedded in the URL.
    expected = base64.b64encode(b":pat-abc").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"
    assert "pat-abc" not in url
    # Auth routed for the ADO org/project/repo.
    assert resolver.calls[0]["org"] == "contoso"
    assert resolver.calls[0]["path"] == "contoso/platform/tools"
    assert resolver.calls[0]["unauth_first"] is False


def test_fetch_ado_bearer_context_sends_bearer_header() -> None:
    captured_headers: list[dict] = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        captured_headers.append(dict(headers or {}))
        return _fake_response(200, text=json.dumps(_MANIFEST))

    resolver = _FakeResolver(
        token="jwt-xyz",
        git_env={"GIT_CONFIG_VALUE_0": "Authorization: Bearer jwt-xyz"},
    )
    with patch("apm_cli.marketplace.client._http_get", side_effect=fake_get):
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=_ado_host_info(),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    assert captured_headers[0]["Authorization"] == "Bearer jwt-xyz"


def test_fetch_ado_404_returns_none_without_clone() -> None:
    def fake_get(url, headers=None, timeout=None, **kwargs):
        return _fake_response(404)

    resolver = _FakeResolver(token="pat-abc")
    with (
        patch("apm_cli.marketplace.client._http_get", side_effect=fake_get),
        patch("apm_cli.marketplace.client._fetch_git") as git_fallback,
    ):
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=_ado_host_info(),
            auth_resolver=resolver,
        )

    assert result is None
    git_fallback.assert_not_called()


def test_fetch_ado_legacy_visualstudio_host() -> None:
    captured: list[str] = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        captured.append(url)
        return _fake_response(200, text=json.dumps(_MANIFEST))

    src = _ado_source(url="https://contoso.visualstudio.com/platform/_git/tools")
    resolver = _FakeResolver(token="pat-abc")
    with patch("apm_cli.marketplace.client._http_get", side_effect=fake_get):
        result = _fetch_ado(
            src,
            "marketplace.json",
            host_info=SimpleNamespace(host="contoso.visualstudio.com", kind="ado"),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    # Legacy hosts carry the org in the subdomain, not as a path segment.
    _assert_ado_items_url(
        captured[0],
        hostname="contoso.visualstudio.com",
        path="/platform/_apis/git/repositories/tools/items",
    )


# ---------------------------------------------------------------------------
# _fetch_ado -- generic-git fallback (no regression)
# ---------------------------------------------------------------------------


def test_fetch_ado_signin_html_falls_back_to_git() -> None:
    """ADO 200 + text/html sign-in page (#1671) -> generic-git clone."""

    def fake_get(url, headers=None, timeout=None, **kwargs):
        return _fake_response(200, text="<html>sign in</html>", content_type="text/html")

    resolver = _FakeResolver(token=None)  # anonymous -> no bearer fallback
    with (
        patch("apm_cli.marketplace.client._http_get", side_effect=fake_get),
        patch("apm_cli.marketplace.client._fetch_git", return_value=_MANIFEST) as git_fallback,
    ):
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=_ado_host_info(),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    git_fallback.assert_called_once()


def test_fetch_ado_network_error_falls_back_to_git() -> None:
    def fake_get(url, headers=None, timeout=None, **kwargs):
        raise client_mod.requests.exceptions.ConnectionError("offline")

    resolver = _FakeResolver(token="pat-abc")
    with (
        patch("apm_cli.marketplace.client._http_get", side_effect=fake_get),
        patch("apm_cli.marketplace.client._fetch_git", return_value=_MANIFEST) as git_fallback,
    ):
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=_ado_host_info(),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    git_fallback.assert_called_once()


def test_fetch_ado_non_decomposable_url_uses_git_directly() -> None:
    """A URL without org/project/repo cannot be REST-fetched -> clone path."""
    bad = _ado_source(url="https://dev.azure.com/contoso")  # no _git marker
    resolver = _FakeResolver(token="pat-abc")
    with (
        patch("apm_cli.marketplace.client._http_get") as http_get,
        patch("apm_cli.marketplace.client._fetch_git", return_value=_MANIFEST) as git_fallback,
    ):
        result = _fetch_ado(
            bad,
            "marketplace.json",
            host_info=_ado_host_info(),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    http_get.assert_not_called()
    git_fallback.assert_called_once()


def test_fetch_ado_invalid_json_falls_back_to_git() -> None:
    def fake_get(url, headers=None, timeout=None, **kwargs):
        return _fake_response(200, text="not json{{")

    resolver = _FakeResolver(token="pat-abc")
    with (
        patch("apm_cli.marketplace.client._http_get", side_effect=fake_get),
        patch("apm_cli.marketplace.client._fetch_git", return_value=_MANIFEST) as git_fallback,
    ):
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=_ado_host_info(),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    git_fallback.assert_called_once()


# ---------------------------------------------------------------------------
# Dispatch + real AuthResolver wiring (parity with GitLab fetcher tests)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_slow_git_credential():
    """Real ``resolve()`` must not block on the git-credential helper."""
    with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
        yield


def test_fetch_file_dispatches_ado_kind_through_rest() -> None:
    """End-to-end: ``_fetch_file`` routes an ADO source to the REST items API."""
    captured: list[tuple[str, dict]] = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        captured.append((url, dict(headers or {})))
        return _fake_response(200, text=json.dumps(_MANIFEST))

    with (
        patch.dict(os.environ, {"ADO_APM_PAT": "pat-real"}, clear=False),
        patch("apm_cli.marketplace.client._http_get", side_effect=fake_get),
    ):
        resolver = AuthResolver()
        result = client_mod._fetch_file(_ado_source(), "marketplace.json", auth_resolver=resolver)

    assert result == _MANIFEST
    assert len(captured) == 1
    url, headers = captured[0]
    parsed = urlparse(url)
    assert parsed.path == "/contoso/platform/_apis/git/repositories/tools/items"
    expected = base64.b64encode(b":pat-real").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"
    # Proxy/GitHub-only path must not be consulted for ADO.
    assert "/repos/" not in url
    assert "/api/v4/" not in url


def test_fetch_ado_rest_auth_failure_then_git_fallback_via_resolver() -> None:
    """A non-404 HTTP error bubbles out of try_with_fallback -> git fallback."""

    def fake_get(url, headers=None, timeout=None, **kwargs):
        return _fake_response(403, text="forbidden")

    # Pin the AAD bearer provider to "unavailable" so the PAT 403 propagates
    # deterministically instead of shelling out to a real ``az`` on the host.
    no_bearer = MagicMock()
    no_bearer.is_available.return_value = False

    with (
        patch.dict(os.environ, {"ADO_APM_PAT": "pat-real"}, clear=False),
        patch("apm_cli.core.azure_cli.get_bearer_provider", return_value=no_bearer),
        patch("apm_cli.marketplace.client._http_get", side_effect=fake_get),
        patch("apm_cli.marketplace.client._fetch_git", return_value=_MANIFEST) as git_fallback,
    ):
        resolver = AuthResolver()
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=AuthResolver.classify_host("dev.azure.com"),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    git_fallback.assert_called_once()


def test_fetch_ado_signin_page_triggers_bearer_fallback_before_clone() -> None:
    """A stale PAT hitting the #1671 sign-in page must retry with AAD bearer.

    Regression guard: the sign-in MarketplaceFetchError message must contain an
    ``is_ado_auth_failure_signal`` keyword so ``try_with_fallback`` attempts the
    AAD bearer before ``_fetch_ado`` degrades to a clone. With the bearer
    available and accepted, the REST path succeeds and ``_fetch_git`` is never
    called.
    """
    bearer_provider = MagicMock()
    bearer_provider.is_available.return_value = True
    bearer_provider.get_bearer_token.return_value = "jwt-fallback"

    def fake_get(url, headers=None, timeout=None, **kwargs):
        auth = (headers or {}).get("Authorization", "")
        if auth.startswith("Bearer "):
            # Bearer retry succeeds with the real manifest.
            assert auth == "Bearer jwt-fallback"
            return _fake_response(200, text=json.dumps(_MANIFEST))
        # The stale PAT (Basic) gets the HTML sign-in page.
        return _fake_response(200, text="<html>sign in</html>", content_type="text/html")

    with (
        patch.dict(os.environ, {"ADO_APM_PAT": "stale-pat"}, clear=False),
        patch("apm_cli.core.azure_cli.get_bearer_provider", return_value=bearer_provider),
        patch("apm_cli.marketplace.client._http_get", side_effect=fake_get),
        patch("apm_cli.marketplace.client._fetch_git") as git_fallback,
    ):
        resolver = AuthResolver()
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=AuthResolver.classify_host("dev.azure.com"),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    bearer_provider.get_bearer_token.assert_called_once()
    git_fallback.assert_not_called()


def test_fetch_ado_rest_raises_is_swallowed_into_fallback() -> None:
    """A MarketplaceFetchError raised inside the operation triggers fallback."""
    sentinel = MarketplaceFetchError("acme", "boom")

    def fake_get(url, headers=None, timeout=None, **kwargs):
        raise sentinel

    resolver = _FakeResolver(token="pat-abc")
    with (
        patch("apm_cli.marketplace.client._http_get", side_effect=fake_get),
        patch("apm_cli.marketplace.client._fetch_git", return_value=_MANIFEST) as git_fallback,
    ):
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=_ado_host_info(),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    git_fallback.assert_called_once()
