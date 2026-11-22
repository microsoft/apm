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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.marketplace import client as client_mod
from apm_cli.marketplace.client import _fetch_ado
from apm_cli.marketplace.models import MarketplaceSource
from tests.utils.scripted_http import (
    ScriptedTransport,
    json_body,
    raising,
    status,
    text_body,
)

pytestmark = pytest.mark.unit

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

    def try_with_fallback(
        self,
        host,
        operation,
        *,
        org=None,
        port=None,
        path=None,
        unauth_first=False,
        base_env=None,
    ):
        self.calls.append(
            {
                "host": host,
                "org": org,
                "port": port,
                "path": path,
                "unauth_first": unauth_first,
                "base_env": base_env,
            }
        )
        return operation(self.token, self.git_env)

    def hardened_git_base_env(self) -> dict:
        return AuthResolver().hardened_git_base_env()


def _sent_headers(request) -> dict:
    """Headers on the wire; ``.netrc`` credentials are never attached."""
    return dict(request.headers)


@pytest.fixture
def transport():
    """Answer ``requests`` at the socket boundary; the owner's session runs for real."""
    scripted = ScriptedTransport()
    with scripted.installed():
        yield scripted


class _GitCheckout:
    """Clone boundary: ``GitCache.get_checkout`` returns a real checkout directory.

    ``_fetch_git`` itself runs for real -- rewrite-safety validation, credential
    routing and reading the manifest file from the checkout.
    """

    def __init__(self, root: Path, manifest: dict | None) -> None:
        self.root = root
        self.manifest = manifest
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, _cache, url, ref, **_kwargs) -> Path:
        self.calls.append((url, ref))
        checkout = self.root / f"checkout-{len(self.calls)}"
        checkout.mkdir(parents=True)
        if self.manifest is not None:
            (checkout / "marketplace.json").write_text(json.dumps(self.manifest), "utf-8")
        return checkout


@pytest.fixture
def git_checkout(tmp_path, monkeypatch):
    """Serve the generic-git fallback from a real checkout holding ``_MANIFEST``.

    ``_fetch_git`` constructs a real ``GitCache``, which prunes staging
    directories, so the cache root is pinned to this test's directory.
    """
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "apm-cache"))
    checkout = _GitCheckout(tmp_path / "git", _MANIFEST)
    with patch(
        "apm_cli.cache.git_cache.GitCache.get_checkout", autospec=True, side_effect=checkout
    ):
        yield checkout


# ---------------------------------------------------------------------------
# _fetch_ado -- REST fast path
# ---------------------------------------------------------------------------


def test_fetch_ado_uses_rest_items_api_not_clone(transport, git_checkout) -> None:
    transport.always(json_body(_MANIFEST))
    resolver = _FakeResolver(
        token="pat-abc",
        git_env=AuthResolver._build_git_env("pat-abc", host_kind="ado", base_env={}),
    )
    result = _fetch_ado(
        _ado_source(),
        "marketplace.json",
        host_info=_ado_host_info(),
        auth_resolver=resolver,
    )

    assert result == _MANIFEST
    assert len(transport.requests) == 1
    request = transport.requests[0]
    _assert_ado_items_url(
        request.url,
        hostname="dev.azure.com",
        path="/contoso/platform/_apis/git/repositories/tools/items",
    )
    # PAT routed as HTTP Basic, never embedded in the URL.
    expected = base64.b64encode(b":pat-abc").decode("ascii")
    assert _sent_headers(request)["Authorization"] == f"Basic {expected}"
    assert "pat-abc" not in request.url
    assert git_checkout.calls == []
    # Auth routed for the ADO org/project/repo.
    assert resolver.calls[0]["org"] == "contoso"
    assert resolver.calls[0]["path"] == "contoso/platform/_git/tools"
    assert resolver.calls[0]["unauth_first"] is False


def test_fetch_ado_bearer_context_sends_bearer_header(transport) -> None:
    transport.always(json_body(_MANIFEST))
    resolver = _FakeResolver(
        token="jwt-xyz",
        git_env={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Bearer jwt-xyz",
        },
    )
    result = _fetch_ado(
        _ado_source(),
        "marketplace.json",
        host_info=_ado_host_info(),
        auth_resolver=resolver,
    )

    assert result == _MANIFEST
    assert _sent_headers(transport.requests[0])["Authorization"] == "Bearer jwt-xyz"


def test_fetch_ado_server_preserves_explicit_port(transport) -> None:
    transport.always(json_body(_MANIFEST))
    source = _ado_source("https://ado.example.test:8443/DefaultCollection/Platform/_git/tools")
    resolver = _FakeResolver(token="pat-abc")

    with patch.dict(os.environ, {"ADO_HOST": "ado.example.test"}, clear=False):
        result = _fetch_ado(
            source,
            "marketplace.json",
            host_info=SimpleNamespace(host="ado.example.test", kind="ado"),
            auth_resolver=resolver,
        )

    assert result == _MANIFEST
    parsed = urlparse(transport.requests[0].url)
    assert parsed.hostname == "ado.example.test"
    assert parsed.port == 8443
    assert resolver.calls[0]["port"] == 8443


def test_fetch_ado_server_rejects_tfs_base_path(transport) -> None:
    source = _ado_source("https://ado.example.test/tfs/DefaultCollection/Platform/_git/tools")
    with patch.dict(os.environ, {"ADO_HOST": "ado.example.test"}, clear=False):
        with pytest.raises(ValueError, match="mounted below '/tfs/'"):
            _fetch_ado(
                source,
                "marketplace.json",
                host_info=SimpleNamespace(host="ado.example.test", kind="ado"),
                auth_resolver=_FakeResolver(token="pat-abc"),
            )
    assert transport.requests == []


def test_fetch_ado_404_returns_none_without_clone(transport, git_checkout) -> None:
    transport.always(status(404))
    result = _fetch_ado(
        _ado_source(),
        "marketplace.json",
        host_info=_ado_host_info(),
        auth_resolver=_FakeResolver(token="pat-abc"),
    )

    assert result is None
    assert len(transport.requests) == 1
    assert git_checkout.calls == []


def test_fetch_ado_legacy_visualstudio_host(transport) -> None:
    transport.always(json_body(_MANIFEST))
    src = _ado_source(url="https://contoso.visualstudio.com/platform/_git/tools")
    result = _fetch_ado(
        src,
        "marketplace.json",
        host_info=SimpleNamespace(host="contoso.visualstudio.com", kind="ado"),
        auth_resolver=_FakeResolver(token="pat-abc"),
    )

    assert result == _MANIFEST
    # Legacy hosts carry the org in the subdomain, not as a path segment.
    _assert_ado_items_url(
        transport.requests[0].url,
        hostname="contoso.visualstudio.com",
        path="/platform/_apis/git/repositories/tools/items",
    )


# ---------------------------------------------------------------------------
# _fetch_ado -- generic-git fallback (no regression)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "respond",
    [
        pytest.param(text_body("<html>sign in</html>", content_type="text/html"), id="signin-html"),
        pytest.param(
            raising(client_mod.requests.exceptions.ConnectionError("offline")),
            id="network-error",
        ),
        pytest.param(text_body("not json{{", content_type="application/json"), id="invalid-json"),
        pytest.param(status(302), id="redirect-without-location"),
        # An oversized body raises MarketplaceFetchError inside the REST
        # operation; it must degrade to the clone, not escape.
        pytest.param(
            status(200, **{"Content-Type": "application/json", "Content-Length": "11534336"}),
            id="oversized-body",
        ),
    ],
)
def test_fetch_ado_rest_failure_falls_back_to_git(transport, git_checkout, respond) -> None:
    """Sign-in HTML (#1671), network failure, invalid JSON or an oversized body -> clone."""
    transport.always(respond)
    result = _fetch_ado(
        _ado_source(),
        "marketplace.json",
        host_info=_ado_host_info(),
        auth_resolver=_FakeResolver(token=None),
    )

    assert result == _MANIFEST
    assert git_checkout.calls == [(_ADO_URL, "main")]


def test_fetch_ado_non_decomposable_url_uses_git_directly(transport, git_checkout) -> None:
    """A URL without org/project/repo cannot be REST-fetched -> clone path."""
    bad = _ado_source(url="https://dev.azure.com/contoso")  # no _git marker
    result = _fetch_ado(
        bad,
        "marketplace.json",
        host_info=_ado_host_info(),
        auth_resolver=_FakeResolver(token="pat-abc"),
    )

    assert result == _MANIFEST
    assert transport.requests == []
    assert git_checkout.calls == [("https://dev.azure.com/contoso", "main")]


# ---------------------------------------------------------------------------
# Dispatch + real AuthResolver wiring (parity with GitLab fetcher tests)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_slow_git_credential():
    """Real ``resolve()`` must not block on the git-credential helper."""
    with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
        yield


def test_fetch_file_dispatches_ado_kind_through_rest(transport) -> None:
    """End-to-end: ``_fetch_file`` routes an ADO source to the REST items API."""
    transport.always(json_body(_MANIFEST))
    with patch.dict(os.environ, {"ADO_APM_PAT": "pat-real"}, clear=False):
        resolver = AuthResolver()
        result = client_mod._fetch_file(_ado_source(), "marketplace.json", auth_resolver=resolver)

    assert result == _MANIFEST
    assert len(transport.requests) == 1
    request = transport.requests[0]
    parsed = urlparse(request.url)
    assert parsed.path == "/contoso/platform/_apis/git/repositories/tools/items"
    expected = base64.b64encode(b":pat-real").decode("ascii")
    assert _sent_headers(request)["Authorization"] == f"Basic {expected}"
    # Proxy/GitHub-only path must not be consulted for ADO.
    assert "/repos/" not in request.url
    assert "/api/v4/" not in request.url


def test_fetch_ado_rest_auth_failure_then_git_fallback_via_resolver(
    transport, git_checkout
) -> None:
    """A non-404 HTTP error bubbles out of try_with_fallback -> git fallback."""
    transport.always(text_body("forbidden", status=403))
    # Pin the AAD bearer provider to "unavailable" so the PAT 403 propagates
    # deterministically instead of shelling out to a real ``az`` on the host.
    no_bearer = MagicMock()
    no_bearer.is_available.return_value = False

    with (
        patch.dict(os.environ, {"ADO_APM_PAT": "pat-real"}, clear=False),
        patch("apm_cli.core.azure_cli.get_bearer_provider", return_value=no_bearer),
    ):
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=AuthResolver.classify_host("dev.azure.com"),
            auth_resolver=AuthResolver(),
        )

    assert result == _MANIFEST
    assert len(git_checkout.calls) == 1


def test_fetch_ado_signin_page_triggers_bearer_fallback_before_clone(
    transport, git_checkout
) -> None:
    """A stale PAT hitting the #1671 sign-in page must retry with AAD bearer.

    Regression guard: the canonical classifier treats the HTML sign-in page as
    an auth failure so ``try_with_fallback`` attempts the AAD bearer before
    ``_fetch_ado`` degrades to a clone. With the bearer available and
    accepted, the REST path succeeds and no clone happens.
    """
    bearer_provider = MagicMock()
    bearer_provider.is_available.return_value = True
    bearer_provider.get_bearer_token.return_value = "jwt-fallback"
    transport.on(
        lambda request: request.headers.get("Authorization") == "Bearer jwt-fallback",
        json_body(_MANIFEST),
    ).always(text_body("<html>sign in</html>", content_type="text/html"))

    with (
        patch.dict(os.environ, {"ADO_APM_PAT": "stale-pat"}, clear=False),
        patch("apm_cli.core.azure_cli.get_bearer_provider", return_value=bearer_provider),
    ):
        result = _fetch_ado(
            _ado_source(),
            "marketplace.json",
            host_info=AuthResolver.classify_host("dev.azure.com"),
            auth_resolver=AuthResolver(),
        )

    assert result == _MANIFEST
    expected_basic = "Basic " + base64.b64encode(b":stale-pat").decode()
    assert [_sent_headers(r)["Authorization"] for r in transport.requests] == [
        expected_basic,
        "Bearer jwt-fallback",
    ]
    bearer_provider.get_bearer_token.assert_called_once()
    assert git_checkout.calls == []


def test_ado_rest_fetch_sends_chain_credential_through_real_http_get() -> None:
    """Unpatched transport: the chain credential reaches the server as a header.

    ``_http_get`` and the redirect owner run for real against a loopback
    stub. The PAT step gets a sign-in redirect, which must be refused (not
    followed) and escalated; the bearer step then succeeds.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[tuple[str, str]] = []

    class _Stub(BaseHTTPRequestHandler):
        def do_GET(self):
            auth = self.headers.get("Authorization", "")
            seen.append((self.path, auth))
            if auth == "Bearer jwt-fallback":
                body = json.dumps(_MANIFEST).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header("Location", "https://app.vssps.visualstudio.com/_signin?realm=x")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stub_url = f"http://127.0.0.1:{server.server_address[1]}/_apis/git/items?path=marketplace.json"

    bearer_provider = MagicMock()
    bearer_provider.is_available.return_value = True
    bearer_provider.get_bearer_token.return_value = "jwt-fallback"
    helper_store = MagicMock()
    helper_store.resolve_git_helper_credential.return_value = None
    try:
        with (
            patch.dict(os.environ, {"ADO_APM_PAT": "stale-pat"}, clear=False),
            patch("apm_cli.core.azure_cli.get_bearer_provider", return_value=bearer_provider),
            patch("apm_cli.utils.github_host.build_ado_api_url", return_value=stub_url),
        ):
            result = client_mod._fetch_ado_rest(
                _ado_source(),
                "marketplace.json",
                org="contoso",
                project="platform",
                repo="tools",
                host="dev.azure.com",
                port=None,
                auth_resolver=AuthResolver(token_manager=helper_store),
            )
    finally:
        server.shutdown()
        server.server_close()

    assert result == _MANIFEST
    expected_basic = "Basic " + base64.b64encode(b":stale-pat").decode()
    assert [auth for _path, auth in seen] == [expected_basic, "Bearer jwt-fallback"]
