"""Tests for marketplace auth helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apm_cli.marketplace.auth_helpers import resolve_auth_for_host, resolve_token_for_host


def test_resolve_token_for_host_offline_returns_none_without_resolver() -> None:
    """Offline checks must not touch AuthResolver."""
    with patch("apm_cli.core.auth.AuthResolver") as mock_resolver:
        assert resolve_token_for_host("gitlab.example.com", offline=True) is None

    mock_resolver.assert_not_called()


def test_resolve_token_for_host_returns_token_from_supplied_resolver() -> None:
    """A supplied resolver is used directly and its token is returned."""
    resolver = MagicMock()
    resolver.resolve.return_value = SimpleNamespace(token="glpat-token", source="TEST")

    token = resolve_token_for_host(
        "gitlab.example.com",
        org="group",
        auth_resolver=resolver,
    )

    assert token == "glpat-token"
    resolver.resolve.assert_called_once_with("gitlab.example.com", org="group")


def test_resolve_auth_for_host_preserves_bearer_scheme() -> None:
    """The context-preserving helper returns the resolver result unchanged."""
    resolver = MagicMock()
    context = SimpleNamespace(
        token="ado-bearer",
        source="AAD_BEARER_AZ_CLI",
        auth_scheme="bearer",
    )
    resolver.resolve.return_value = context

    resolved = resolve_auth_for_host(
        "dev.azure.com",
        org="contoso",
        auth_resolver=resolver,
    )

    assert resolved is context
    assert resolved.auth_scheme == "bearer"
    resolver.resolve.assert_called_once_with("dev.azure.com", org="contoso")


def test_resolve_token_for_host_returns_none_when_resolver_raises() -> None:
    """Auth failures must fall back to ambient git credentials."""
    resolver = MagicMock()
    resolver.resolve.side_effect = RuntimeError("auth unavailable")

    assert resolve_token_for_host("gitlab.example.com", auth_resolver=resolver) is None


def test_resolve_token_for_host_creates_resolver_when_not_supplied() -> None:
    """Callers may let the helper create AuthResolver lazily."""
    with patch("apm_cli.core.auth.AuthResolver") as mock_resolver:
        mock_resolver.return_value.resolve.return_value = SimpleNamespace(
            token="ghp-token",
            source="TEST",
        )

        token = resolve_token_for_host("github.example.com")

    assert token == "ghp-token"
    mock_resolver.assert_called_once_with()
    mock_resolver.return_value.resolve.assert_called_once_with("github.example.com")
