"""Hosted catalog credential binding, scope and denied access (#3300)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apm_cli.marketplace import client as client_mod
from apm_cli.marketplace.catalog_binding import require_binding_available
from apm_cli.marketplace.errors import (
    MarketplaceAccessDeniedError,
    MarketplaceBindingConflictError,
    MarketplaceCredentialError,
)
from apm_cli.marketplace.models import MarketplaceSource

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-marketplace-catalog-credentials")]

_URL = "https://catalog.example.com/team/marketplace.json"
_CATALOG = json.dumps({"name": "corp", "plugins": [{"name": "p", "source": "./p"}]}).encode()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(tmp_path / ".apm"))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
    for prefix in ("TOKEN", "USER", "PASS"):
        monkeypatch.delenv(f"APM_MARKETPLACE_{prefix}_CORP", raising=False)


def _response(status: int, *, body: bytes = b"", headers: dict[str, str] | None = None):
    return SimpleNamespace(
        status_code=status,
        headers=dict(headers or {}),
        iter_content=lambda chunk_size: iter([body]),
        raise_for_status=lambda: None,
        close=lambda: None,
    )


class _Session:
    cookies = None

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], object]] = []

    def request(self, method, url, headers=None, **kwargs):
        self.calls.append((url, dict(headers or {}), kwargs.get("auth")))
        return self._responses.pop(0)


def _install(monkeypatch, *responses) -> _Session:
    session = _Session(*responses)
    # Replace only the I/O boundary: the shared session's transport call.
    monkeypatch.setattr(client_mod._HTTP_SESSION, "request", session.request)
    return session


def _bound(name: str = "corp", url: str = _URL) -> MarketplaceSource:
    return MarketplaceSource(name=name, url=url, ref="", path="", credential_bound=True)


def test_bound_catalog_sends_its_bearer_token_without_ambient_netrc(monkeypatch) -> None:
    monkeypatch.setenv("APM_MARKETPLACE_TOKEN_CORP", "tok-1")
    session = _install(monkeypatch, _response(200, body=_CATALOG))

    manifest = client_mod.fetch_marketplace(_bound(), force_refresh=True)

    assert [p.name for p in manifest.plugins] == ["p"]
    ((_url, headers, auth),) = session.calls
    assert headers["Authorization"] == "Bearer tok-1"
    assert auth is not None  # explicit no-op auth: requests never loads .netrc


def test_unbound_catalog_never_selects_a_credential(monkeypatch) -> None:
    """A name taken from the catalog's own manifest is not a binding."""
    monkeypatch.setenv("APM_MARKETPLACE_TOKEN_CORP", "tok-1")
    session = _install(monkeypatch, _response(200, body=_CATALOG))
    source = MarketplaceSource(name="corp", url=_URL, ref="", path="")

    client_mod.fetch_marketplace(source, force_refresh=True)

    ((_url, headers, auth),) = session.calls
    assert "Authorization" not in headers
    assert auth is not None


@pytest.mark.parametrize("status", [401, 403])
def test_denied_access_names_the_variable_and_never_serves_stale_cache(
    monkeypatch, status: int
) -> None:
    source = _bound()
    client_mod._write_cache(client_mod._cache_key(source), json.loads(_CATALOG))
    cached = client_mod._read_stale_cache(client_mod._cache_key(source))
    _install(monkeypatch, _response(status))

    with pytest.raises(MarketplaceAccessDeniedError) as excinfo:
        client_mod.fetch_marketplace(source, force_refresh=True)

    assert f"HTTP {status}" in str(excinfo.value)
    assert "APM_MARKETPLACE_TOKEN_CORP" in str(excinfo.value)
    assert client_mod._read_stale_cache(client_mod._cache_key(source)) == cached


def test_denied_refresh_keeps_the_cache_and_is_not_reported_fresh(monkeypatch) -> None:
    monkeypatch.setenv("APM_MARKETPLACE_TOKEN_CORP", "tok-1")
    source = _bound()
    key = client_mod._cache_key(source)
    client_mod._write_cache(key, json.loads(_CATALOG))
    meta_before = client_mod._read_stale_meta(key)
    _install(monkeypatch, _response(401))

    with pytest.raises(MarketplaceAccessDeniedError, match="APM_MARKETPLACE_TOKEN_CORP"):
        client_mod.fetch_marketplace(source, require_fresh=True)

    assert client_mod._read_stale_meta(key) == meta_before


def test_sign_in_redirect_is_denied_access(monkeypatch) -> None:
    monkeypatch.setenv("APM_MARKETPLACE_TOKEN_CORP", "tok-1")
    session = _install(
        monkeypatch,
        _response(302, headers={"Location": "https://sso.example.net/login"}),
        _response(200, body=b"<html>sign in</html>", headers={"Content-Type": "text/html"}),
    )

    with pytest.raises(MarketplaceAccessDeniedError, match="sign-in page"):
        client_mod.fetch_marketplace(_bound(), force_refresh=True)

    assert "Authorization" not in session.calls[1][1]


def test_out_of_scope_redirect_is_followed_without_the_credential(monkeypatch) -> None:
    monkeypatch.setenv("APM_MARKETPLACE_TOKEN_CORP", "tok-1")
    session = _install(
        monkeypatch,
        _response(302, headers={"Location": "/elsewhere/marketplace.json"}),
        _response(200, body=_CATALOG),
    )

    client_mod.fetch_marketplace(_bound(), force_refresh=True)

    assert [("Authorization" in headers) for _url, headers, _auth in session.calls] == [
        True,
        False,
    ]


def test_malformed_token_fails_closed_without_network(monkeypatch) -> None:
    monkeypatch.setenv("APM_MARKETPLACE_TOKEN_CORP", "bad token-secret")
    session = _install(monkeypatch)

    with pytest.raises(MarketplaceCredentialError) as excinfo:
        client_mod.fetch_marketplace(_bound(), force_refresh=True)

    assert "token-secret" not in str(excinfo.value)
    assert session.calls == []


def test_binding_conflict_fails_closed() -> None:

    with pytest.raises(MarketplaceBindingConflictError, match="already bound"):
        require_binding_available("CORP", "https://evil.example.com/marketplace.json", [_bound()])
    require_binding_available("corp", _URL, [_bound()])


def test_binding_rejects_an_ambiguous_peer() -> None:
    with pytest.raises(MarketplaceCredentialError, match=r"'corp\.main' and 'corp-main'"):
        require_binding_available("corp.main", _URL, [_bound(name="corp-main")])


def test_credential_binding_round_trips_only_for_hosted_catalogs() -> None:
    assert MarketplaceSource.from_dict(_bound().to_dict()).credential_bound is True
    git_source = MarketplaceSource(name="g", owner="o", repo="r", credential_bound=True)
    assert git_source.credential_bound is False
    assert "credential_bound" not in git_source.to_dict()
