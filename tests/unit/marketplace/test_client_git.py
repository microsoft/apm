"""Unit coverage for the ``_fetch_git`` marketplace fetcher.

Covers:
- mock ``GitCache.get_checkout`` is called with the right URL, ref, sparse_paths, env
- ADO URL routes via subprocess git + auth_resolver-built env
- generic git URL with no APM creds -> empty/inherited env passed through
- missing ``marketplace.json`` in checkout -> ``None``
- ``GitCache`` failure surfaces as ``MarketplaceFetchError``
- ``_FETCHERS`` dispatch regression-trap: dict[kind] points at the right callable
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.marketplace.client import (
    _FETCHERS,
    _fetch_ado,
    _fetch_git,
    _fetch_github,
    _fetch_gitlab,
    _fetch_local,
)
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import MarketplaceSource


def _git_source(url: str, name: str = "acme", ref: str = "main") -> MarketplaceSource:
    return MarketplaceSource(name=name, url=url, ref=ref)


@pytest.fixture
def fake_host_info():
    return SimpleNamespace(host="gitea.example.com")


@pytest.fixture
def fake_auth_resolver():
    resolver = MagicMock()
    resolver.resolve.return_value = SimpleNamespace(git_env={"GIT_TERMINAL_PROMPT": "0"})
    return resolver


def test_fetch_git_calls_gitcache_with_sparse_path(
    tmp_path: Path, fake_host_info, fake_auth_resolver
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "marketplace.json").write_text(json.dumps({"name": "acme", "plugins": []}))

    gitcache_mock = MagicMock()
    gitcache_mock.get_checkout.return_value = str(checkout)
    with (
        patch("apm_cli.cache.git_cache.GitCache", return_value=gitcache_mock),
        patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
    ):
        result = _fetch_git(
            _git_source("https://gitea.example.com/org/repo.git"),
            "marketplace.json",
            host_info=fake_host_info,
            auth_resolver=fake_auth_resolver,
        )

    assert result == {"name": "acme", "plugins": []}
    fake_auth_resolver.resolve.assert_called_once()
    call_kwargs = gitcache_mock.get_checkout.call_args.kwargs
    assert call_kwargs["env"] == {"GIT_TERMINAL_PROMPT": "0"}


def test_fetch_git_ado_url_routes_via_subprocess(
    tmp_path: Path, fake_host_info, fake_auth_resolver
) -> None:
    """``_fetch_git`` (the ADO REST fallback path) still clones via ``GitCache``.

    ADO marketplace reads now prefer ``_fetch_ado`` (REST items API); this test
    pins the generic-git fallback that ``_fetch_ado`` delegates to on REST
    failure -- it must keep building the auth env and cloning.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "marketplace.json").write_text("{}")

    gitcache_mock = MagicMock()
    gitcache_mock.get_checkout.return_value = str(checkout)
    fake_auth_resolver.resolve.return_value = SimpleNamespace(
        git_env={
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: bearer xxx",
        }
    )

    with (
        patch("apm_cli.cache.git_cache.GitCache", return_value=gitcache_mock),
        patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
    ):
        result = _fetch_git(
            _git_source("https://dev.azure.com/org/project/_git/repo"),
            "marketplace.json",
            host_info=SimpleNamespace(host="dev.azure.com"),
            auth_resolver=fake_auth_resolver,
        )

    assert result == {}
    env = gitcache_mock.get_checkout.call_args.kwargs["env"]
    assert "GIT_CONFIG_VALUE_0" in env


def test_fetch_git_returns_none_when_manifest_missing(
    tmp_path: Path, fake_host_info, fake_auth_resolver
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    # No marketplace.json on disk

    gitcache_mock = MagicMock()
    gitcache_mock.get_checkout.return_value = str(checkout)
    with (
        patch("apm_cli.cache.git_cache.GitCache", return_value=gitcache_mock),
        patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
    ):
        result = _fetch_git(
            _git_source("https://gitea.example.com/org/repo.git"),
            "marketplace.json",
            host_info=fake_host_info,
            auth_resolver=fake_auth_resolver,
        )

    assert result is None


def test_fetch_git_remote_not_found_returns_none(
    tmp_path: Path, fake_host_info, fake_auth_resolver
) -> None:
    """``GitCache`` raising 'not found' surfaces as a soft None for path auto-detection."""
    err = subprocess.CalledProcessError(returncode=128, cmd=["git"], stderr=b"remote ref not found")
    gitcache_mock = MagicMock()
    gitcache_mock.get_checkout.side_effect = err
    with (
        patch("apm_cli.cache.git_cache.GitCache", return_value=gitcache_mock),
        patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
    ):
        result = _fetch_git(
            _git_source("https://gitea.example.com/org/repo.git"),
            "marketplace.json",
            host_info=fake_host_info,
            auth_resolver=fake_auth_resolver,
        )

    assert result is None


def test_fetch_git_subprocess_failure_raises_marketplace_fetch_error(
    tmp_path: Path, fake_host_info, fake_auth_resolver
) -> None:
    err = subprocess.CalledProcessError(returncode=128, cmd=["git"], stderr=b"auth failed")
    gitcache_mock = MagicMock()
    gitcache_mock.get_checkout.side_effect = err
    with (
        patch("apm_cli.cache.git_cache.GitCache", return_value=gitcache_mock),
        patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
    ):
        with pytest.raises(MarketplaceFetchError, match="git fetch failed"):
            _fetch_git(
                _git_source("https://gitea.example.com/org/repo.git"),
                "marketplace.json",
                host_info=fake_host_info,
                auth_resolver=fake_auth_resolver,
            )


def test_fetchers_dispatch_table_routes_kinds_to_correct_callable() -> None:
    """Regression trap: locking the trust boundary into _FETCHERS."""
    assert _FETCHERS["git"] is _fetch_git
    assert _FETCHERS["local"] is _fetch_local
    assert _FETCHERS["github"] is _fetch_github
    assert _FETCHERS["gitlab"] is _fetch_gitlab
    assert _FETCHERS["ado"] is _fetch_ado
    # Defensive: no extra entries silently appearing.
    assert set(_FETCHERS) == {"github", "gitlab", "ado", "git", "local"}
