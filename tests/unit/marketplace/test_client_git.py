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

from apm_cli.commands.marketplace import _parse_marketplace_source
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
    return SimpleNamespace(host="gitea.example.com", kind="generic")


@pytest.fixture
def fake_auth_resolver():
    resolver = MagicMock()
    resolver.resolve.return_value = SimpleNamespace(git_env={"GIT_TERMINAL_PROMPT": "0"})
    resolver.resolve_for_remote.return_value = resolver.resolve.return_value
    resolver.git_env_for_remote.side_effect = lambda auth_ctx, _remote_url: auth_ctx.git_env
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
    fake_auth_resolver.resolve_for_remote.assert_called_once()
    call_kwargs = gitcache_mock.get_checkout.call_args.kwargs
    assert call_kwargs["env"] == {"GIT_TERMINAL_PROMPT": "0"}
    fake_auth_resolver.git_env_for_remote.assert_called_once()


def test_fetch_git_preserves_ssh_protocol_url_with_port(tmp_path: Path, fake_auth_resolver) -> None:
    raw = "ssh://git@github.com:2222/org/repo.git"
    url, kind, host = _parse_marketplace_source(raw, host_flag=None)
    source = _git_source(url)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "marketplace.json").write_text(
        json.dumps({"name": "acme", "plugins": []}), encoding="utf-8"
    )

    gitcache_mock = MagicMock()
    gitcache_mock.get_checkout.return_value = str(checkout)
    with (
        patch("apm_cli.cache.git_cache.GitCache", return_value=gitcache_mock),
        patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
    ):
        result = _fetch_git(
            source,
            "marketplace.json",
            host_info=SimpleNamespace(host=host),
            auth_resolver=fake_auth_resolver,
        )

    assert kind == "git"
    assert host == "github.com"
    assert source.kind == "git"
    assert source.to_dict()["url"] == raw
    assert result == {"name": "acme", "plugins": []}
    assert gitcache_mock.get_checkout.call_args.args[0] == raw
    fake_auth_resolver.resolve.assert_not_called()
    fake_auth_resolver.resolve_for_remote.assert_called_once()
    fake_auth_resolver.git_env_for_remote.assert_called_once()


@pytest.mark.parametrize(
    "raw",
    [
        "ssh://git@github.com:2222/org/repo.git",
        "git@gitea.example.com:org/repo.git",
    ],
)
def test_fetch_git_ssh_does_not_forward_http_credentials(tmp_path: Path, raw: str) -> None:
    source = _git_source(raw)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "marketplace.json").write_text("{}", encoding="utf-8")

    credential_env = {
        "GITHUB_APM_PAT": "platform-secret",
        "GITHUB_APM_PAT_ORG": "org-secret",
        "GIT_TOKEN": "git-secret",
        "GIT_HTTP_EXTRAHEADER": "Authorization: Bearer header-secret",
        "GIT_CONFIG_PARAMETERS": "'http.extraheader=Authorization: Bearer config-secret'",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": "Authorization: Bearer indexed-secret",
        "GIT_ASKPASS": "echo",
        "GIT_TERMINAL_PROMPT": "0",
    }
    auth_resolver = MagicMock()
    auth_resolver.resolve_for_remote.return_value = SimpleNamespace(git_env=credential_env)
    auth_resolver.git_env_for_remote.return_value = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=30",
        "SSH_ASKPASS_REQUIRE": "never",
        "GITHUB_APM_PAT": "",
        "GITHUB_APM_PAT_ORG": "",
    }

    gitcache_mock = MagicMock()
    gitcache_mock.get_checkout.return_value = str(checkout)
    with (
        patch("apm_cli.cache.git_cache.GitCache", return_value=gitcache_mock),
        patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
    ):
        result = _fetch_git(
            source,
            "marketplace.json",
            host_info=SimpleNamespace(
                host="github.com" if raw.startswith("ssh://") else "gitea.example.com"
            ),
            auth_resolver=auth_resolver,
        )

    assert result == {}
    auth_resolver.resolve.assert_not_called()
    auth_resolver.resolve_for_remote.assert_called_once()
    auth_resolver.git_env_for_remote.assert_called_once()
    env = gitcache_mock.get_checkout.call_args.kwargs["env"]
    assert "GIT_TOKEN" not in env
    assert "GIT_HTTP_EXTRAHEADER" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert "GIT_ASKPASS" not in env
    assert env["GITHUB_APM_PAT"] == ""
    assert env["GITHUB_APM_PAT_ORG"] == ""
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes -o ConnectTimeout=30"
    assert env["SSH_ASKPASS_REQUIRE"] == "never"


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
    fake_auth_resolver.resolve_for_remote.return_value = fake_auth_resolver.resolve.return_value

    with (
        patch("apm_cli.cache.git_cache.GitCache", return_value=gitcache_mock),
        patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
    ):
        result = _fetch_git(
            _git_source("https://dev.azure.com/org/project/_git/repo"),
            "marketplace.json",
            host_info=SimpleNamespace(host="dev.azure.com", kind="ado"),
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


def test_fetch_git_does_not_render_generic_exception_text(
    tmp_path: Path, fake_host_info, fake_auth_resolver
) -> None:
    """A helper or GitCache error cannot leak through the CLI diagnostic."""
    gitcache_mock = MagicMock()
    gitcache_mock.get_checkout.side_effect = RuntimeError("helper-output-fixture-secret")
    with (
        patch("apm_cli.cache.git_cache.GitCache", return_value=gitcache_mock),
        patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
    ):
        with pytest.raises(MarketplaceFetchError) as raised:
            _fetch_git(
                _git_source("https://gitea.example.com/org/repo.git"),
                "marketplace.json",
                host_info=fake_host_info,
                auth_resolver=fake_auth_resolver,
            )

    assert "helper-output-fixture-secret" not in str(raised.value)
    assert "git fetch failed" in str(raised.value)
    assert "apm marketplace update acme" in str(raised.value)


def test_fetch_git_wraps_https_downgrade_rejection(fake_host_info, fake_auth_resolver) -> None:
    """Transport-policy rejection keeps the actionable marketplace error shape."""
    fake_auth_resolver.git_env_for_remote.side_effect = ValueError(
        "HTTPS Git remote is configured to rewrite to insecure HTTP"
    )

    with pytest.raises(MarketplaceFetchError) as raised:
        _fetch_git(
            _git_source("https://gitea.example.com/org/repo.git"),
            "marketplace.json",
            host_info=fake_host_info,
            auth_resolver=fake_auth_resolver,
        )

    message = str(raised.value)
    assert "HTTPS Git remote was rejected" in message
    assert "apm marketplace update acme" in message


def test_fetchers_dispatch_table_routes_kinds_to_correct_callable() -> None:
    """Regression trap: locking the trust boundary into _FETCHERS."""
    assert _FETCHERS["git"] is _fetch_git
    assert _FETCHERS["local"] is _fetch_local
    assert _FETCHERS["github"] is _fetch_github
    assert _FETCHERS["gitlab"] is _fetch_gitlab
    assert _FETCHERS["ado"] is _fetch_ado
    # Defensive: no extra entries silently appearing.
    assert set(_FETCHERS) == {"github", "gitlab", "ado", "git", "local"}
