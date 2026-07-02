"""Integration coverage for ADO marketplace metadata over the REST items API.

Exercises the public ``fetch_marketplace`` path end-to-end for an Azure DevOps
source (``kind == "ado"``):

- REST items API is used instead of a subprocess clone, with ``ADO_APM_PAT``
  routed as HTTP Basic.
- The result is served from the JSON sidecar cache on the second fetch
  (parity with the GitLab REST fetcher; no second network call).
- A REST/transport failure falls back to the generic-git path with no
  regression, and the parsed manifest is returned.

Fetcher-level unit coverage lives in
``tests/unit/marketplace/test_client_ado.py``.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.marketplace import client as client_mod
from apm_cli.marketplace import registry
from apm_cli.marketplace.client import fetch_marketplace
from apm_cli.marketplace.models import MarketplaceSource

_ADO_URL = "https://dev.azure.com/contoso/platform/_git/tools"
_MANIFEST = {
    "name": "ado-mkt",
    "owner": "contoso",
    "plugins": [
        {"name": "tool-x", "source": "./tools/x", "version": "1.0.0"},
    ],
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = str(tmp_path / ".apm")
    Path(config_dir).mkdir()
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr(registry, "_registry_cache", None)
    # Real resolve() must not block on the git-credential helper.
    monkeypatch.setattr(GitHubTokenManager, "resolve_credential_from_git", lambda *a, **k: None)


def _fake_response(status_code: int, *, text: str = "", content_type: str = "application/json"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"Content-Type": content_type}
    # Marketplace fetches stream the body via resp.iter_content under a byte
    # ceiling, so the double must yield the payload bytes in chunks.
    resp.iter_content = lambda chunk_size=None: iter([text.encode("utf-8")])

    def _raise_for_status():
        if status_code >= 400:
            raise client_mod.requests.exceptions.HTTPError(f"HTTP {status_code}")

    resp.raise_for_status.side_effect = _raise_for_status
    return resp


def test_fetch_marketplace_ado_uses_rest_and_then_sidecar_cache(monkeypatch) -> None:
    monkeypatch.setenv("ADO_APM_PAT", "pat-real")
    source = MarketplaceSource(name="ado-mkt", url=_ADO_URL, ref="main")
    assert source.kind == "ado"

    calls: list[tuple[str, dict]] = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        calls.append((url, dict(headers or {})))
        return _fake_response(200, text=json.dumps(_MANIFEST))

    with patch("apm_cli.marketplace.client._http_get", side_effect=fake_get):
        resolver = AuthResolver()
        first = fetch_marketplace(source, auth_resolver=resolver)
        second = fetch_marketplace(source, auth_resolver=resolver)

    assert first.name == "ado-mkt"
    assert first.find_plugin("tool-x") is not None
    assert second.name == "ado-mkt"
    # Exactly one network call: the second fetch is served from the sidecar cache.
    assert len(calls) == 1
    url, headers = calls[0]
    assert "/_apis/git/repositories/tools/items" in url
    expected = base64.b64encode(b":pat-real").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"
    assert "pat-real" not in url


def test_fetch_marketplace_ado_rest_failure_falls_back_to_git(monkeypatch) -> None:
    monkeypatch.setenv("ADO_APM_PAT", "pat-real")
    source = MarketplaceSource(name="ado-mkt", url=_ADO_URL, ref="main")

    no_bearer = MagicMock()
    no_bearer.is_available.return_value = False

    def fake_get(url, headers=None, timeout=None, **kwargs):
        # Simulate ADO returning a sign-in page (auth insufficient).
        return _fake_response(200, text="<html>sign in</html>", content_type="text/html")

    with (
        patch("apm_cli.core.azure_cli.get_bearer_provider", return_value=no_bearer),
        patch("apm_cli.marketplace.client._http_get", side_effect=fake_get),
        patch("apm_cli.marketplace.client._fetch_git", return_value=_MANIFEST) as git_fallback,
    ):
        resolver = AuthResolver()
        manifest = fetch_marketplace(source, auth_resolver=resolver)

    assert manifest.name == "ado-mkt"
    assert manifest.find_plugin("tool-x") is not None
    git_fallback.assert_called_once()


def test_fetch_marketplace_ado_offline_serves_stale_cache(monkeypatch) -> None:
    """REST + git both fail on a later fetch -> stale sidecar cache is served."""
    monkeypatch.setenv("ADO_APM_PAT", "pat-real")
    source = MarketplaceSource(name="ado-mkt", url=_ADO_URL, ref="main")

    # Warm the cache via a successful REST fetch.
    with patch(
        "apm_cli.marketplace.client._http_get",
        side_effect=lambda *a, **k: _fake_response(200, text=json.dumps(_MANIFEST)),
    ):
        fetch_marketplace(source, auth_resolver=AuthResolver())

    # Expire the cache, then make both REST and git fail; stale cache wins.
    cache_name = client_mod._cache_key(source)
    meta_path = client_mod._cache_meta_path(cache_name)
    with open(meta_path) as f:
        meta = json.load(f)
    meta["fetched_at"] = 0  # force-expire
    with open(meta_path, "w") as f:
        json.dump(meta, f)

    no_bearer = MagicMock()
    no_bearer.is_available.return_value = False

    def boom(*a, **k):
        raise client_mod.requests.exceptions.ConnectionError("offline")

    with (
        patch("apm_cli.core.azure_cli.get_bearer_provider", return_value=no_bearer),
        patch("apm_cli.marketplace.client._http_get", side_effect=boom),
        patch(
            "apm_cli.marketplace.client._fetch_git",
            side_effect=client_mod.MarketplaceFetchError("ado-mkt", "git offline"),
        ),
    ):
        manifest = fetch_marketplace(source, auth_resolver=AuthResolver())

    assert manifest.name == "ado-mkt"
    assert manifest.find_plugin("tool-x") is not None


def test_marketplace_source_ado_cache_key_distinct_from_git(monkeypatch) -> None:
    """ADO and generic-git sidecar files never collide for the same host/name."""
    ado = MarketplaceSource(name="m", url="https://dev.azure.com/o/p/_git/r")
    # A generic-git URL on a different host keeps a distinct, kind-prefixed key.
    git = MarketplaceSource(name="m", url="https://gitea.example.com/o/r.git")
    assert client_mod._cache_key(ado).startswith("ado__")
    assert client_mod._cache_key(git).startswith("git__")
    assert client_mod._cache_key(ado) != client_mod._cache_key(git)
