"""Drive the real Azure DevOps auth chain from a mocked AuthResolver.

Unit tests that mock ``AuthResolver`` (``resolve`` returning a stub context)
still need the canonical chain semantics: classification, PAT -> az bearer ->
helper escalation, and the exact Authorization header per attempt. This
helper wires ``try_with_fallback`` and ``http_authorization_headers`` on the
mock to a real :class:`AuthResolver` whose ``resolve`` reads the mock's stub
context. It never reaches a real az CLI or credential helper: the bearer
provider defaults to unavailable and the helper returns nothing unless a
test sets ``auth.helper_credential``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apm_cli.core.auth import AuthContext, AuthResolver


def _unavailable_provider() -> MagicMock:
    provider = MagicMock()
    provider.is_available.return_value = False
    provider.last_error = None
    return provider


def install_simulated_ado_chain(auth: MagicMock) -> MagicMock:
    """Wire *auth* (a mocked AuthResolver) to run the real ADO chain."""
    auth.bearer_provider = _unavailable_provider()
    auth.helper_credential = None
    auth.http_authorization_headers.side_effect = AuthResolver.http_authorization_headers

    def _try_with_fallback(host: str, operation, **kwargs):
        stub = auth.resolve(host, kwargs.get("org"), port=kwargs.get("port"))
        token = getattr(stub, "token", None)
        scheme = getattr(stub, "auth_scheme", "basic")
        source = getattr(stub, "source", None)
        if not isinstance(source, str) or not source:
            source = (
                "AAD_BEARER_AZ_CLI" if token and scheme == "bearer" else "none" if not token else ""
            )
        context = AuthContext(
            token=token,
            source=source,
            token_type="unknown",
            host_info=AuthResolver.classify_host(host, port=kwargs.get("port")),
            git_env={},
            auth_scheme=scheme if isinstance(scheme, str) else "basic",
        )
        token_manager = MagicMock()
        token_manager.resolve_git_helper_credential.side_effect = lambda *_a, **_k: (
            auth.helper_credential
        )
        resolver = AuthResolver(token_manager=token_manager)
        resolver.resolve = lambda *_a, **_k: context
        with patch("apm_cli.core.azure_cli.get_bearer_provider", return_value=auth.bearer_provider):
            return resolver.try_with_fallback(host, operation, **kwargs)

    auth.try_with_fallback.side_effect = _try_with_fallback
    return auth


class StaticPolicyResolver:
    """Single-attempt resolver presenting one fixed token (``None`` = anonymous)."""

    def __init__(self, token: str | None) -> None:
        self.token = token
        self.calls: list[dict] = []

    def try_with_fallback(self, host: str, operation, **kwargs):
        self.calls.append({"host": host, **kwargs})
        return operation(self.token, {})

    def build_error_context(self, *_args, **_kwargs) -> str:
        return ""

    @staticmethod
    def http_authorization_headers(git_env):
        return AuthResolver.http_authorization_headers(git_env)
