"""Unit coverage for the ``_fetch_git`` marketplace fetcher (pure logic cases)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

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
from apm_cli.utils.git_env import GitUrlRewriteError

pytestmark = pytest.mark.unit


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
    resolver.hardened_git_base_env.return_value = {"GIT_TERMINAL_PROMPT": "0"}
    return resolver


@pytest.mark.parametrize(
    ("reason", "detail", "expected"),
    (
        ("https-downgrade", "HTTPS Git remote must not rewrite to insecure HTTP", "insecure"),
        ("credentials", "Git URL rewrite replacement must not contain credentials", "credentials"),
        (
            "credential-origin",
            "Authenticated Git remote must not rewrite to a different HTTPS origin",
            "changes the remote origin",
        ),
    ),
)
def test_fetch_git_wraps_url_rewrite_rejection(
    fake_host_info,
    fake_auth_resolver,
    reason: str,
    detail: str,
    expected: str,
) -> None:
    """Transport-policy rejection keeps the actionable marketplace error shape."""
    fake_auth_resolver.git_env_for_remote.side_effect = GitUrlRewriteError(reason, detail)

    with pytest.raises(MarketplaceFetchError) as raised:
        _fetch_git(
            _git_source("https://gitea.example.com/org/repo.git"),
            "marketplace.json",
            host_info=fake_host_info,
            auth_resolver=fake_auth_resolver,
        )

    message = str(raised.value)
    assert expected in message
    assert "git config --show-origin --get-regexp" in message
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
