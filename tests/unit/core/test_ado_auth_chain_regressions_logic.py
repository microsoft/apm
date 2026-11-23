"""Hermetic regressions for the Azure DevOps auth chain (#3752, #3758, #3753) (pure logic cases)."""

from __future__ import annotations

import base64

import pytest

import apm_cli.core.azure_cli as azure_cli_module
import apm_cli.core.token_manager as token_manager_module
from apm_cli.core.auth import AuthResolver, _redact_secrets
from apm_cli.core.azure_cli import AzureCliBearerError

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-host-credential-resolution")]


class _FakeProvider:
    """Stand-in for the az CLI bearer provider."""

    def __init__(self, *, available: bool, error: AzureCliBearerError | None = None) -> None:
        self._available = available
        self._error = error
        self.tenant_calls = 0

    def is_available(self) -> bool:
        return self._available

    def get_bearer_token(self) -> str:
        if self._error is not None:
            raise self._error
        raise AzureCliBearerError("not signed in", kind="not_logged_in")

    def get_current_tenant_id(self) -> str | None:
        self.tenant_calls += 1
        return "tenant-0000"

    @property
    def last_error(self) -> AzureCliBearerError | None:
        return self._error


def test_error_context_reports_az_failure_reason_not_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3753: an az token failure is not presented as a rejected bearer token."""
    monkeypatch.delenv("ADO_APM_PAT", raising=False)
    error = AzureCliBearerError(
        "az CLI returned exit code 1",
        kind="not_logged_in",
        stderr="ERROR: AADSTS50076: Due to a configuration change you must use MFA.",
    )
    provider = _FakeProvider(available=True, error=error)
    monkeypatch.setattr(azure_cli_module, "get_bearer_provider", lambda: provider)
    resolver = AuthResolver()

    message = resolver.build_error_context("dev.azure.com", "install", org="contoso")

    assert "AADSTS50076" in message
    assert "returned a bearer token" not in message
    assert "rejected it (HTTP 401)" not in message


def test_secret_redaction_keeps_bearer_token_phrase() -> None:
    """#3753: diagnostics may say 'bearer token' without redacting the words."""
    assert _redact_secrets("ADO bearer token acquisition failed") == (
        "ADO bearer token acquisition failed"
    )
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJmaXh0dXJlIn0.c2lnbmF0dXJlLXZhbHVl"
    assert jwt not in _redact_secrets(f"Authorization: Bearer {jwt}")


def _git_config(env: dict[str, str]) -> list[tuple[str, str]]:
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]


@pytest.mark.parametrize("pat", ["stale-pat", None], ids=["pat-bearer-helper", "tokenless"])
def test_each_ado_step_child_env_carries_only_its_own_credential(monkeypatch, pat) -> None:
    """Each attempt's git env holds exactly its own header and no helper lookup."""
    for name in ("ADO_APM_PAT", "GITHUB_TOKEN", "GITHUB_APM_PAT", "GH_TOKEN", "GIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    if pat:
        monkeypatch.setenv("ADO_APM_PAT", pat)
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token")

    class _Bearer:
        last_error = None

        def is_available(self) -> bool:
            return True

        def get_bearer_token(self) -> str:
            return "az-jwt"

    monkeypatch.setattr(azure_cli_module, "get_bearer_provider", lambda: _Bearer())
    helper = token_manager_module.GitHelperCredential(
        host="dev.azure.com", password="helper-pw", username="me"
    )
    monkeypatch.setattr(
        token_manager_module.GitHubTokenManager,
        "resolve_git_helper_credential",
        staticmethod(lambda *_a, **_k: helper),
    )
    seen: list[tuple[str | None, dict[str, str]]] = []

    def _rejected(token, env):
        seen.append((token, dict(env)))
        raise RuntimeError("fatal: Authentication failed (401)")

    from apm_cli.core.auth_chain import AdoAuthChainExhaustedError

    with pytest.raises(AdoAuthChainExhaustedError, match="Tried in order"):
        AuthResolver().try_with_fallback(
            "dev.azure.com",
            _rejected,
            org="o",
            path="o/p/_git/r",
            remote_url="https://dev.azure.com/o/p/_git/r",
        )

    expected = {
        "stale-pat": "Basic " + base64.b64encode(b":stale-pat").decode(),
        "az-jwt": "Bearer az-jwt",
        "helper-pw": "Basic " + base64.b64encode(b"me:helper-pw").decode(),
    }
    tokens = [token for token, _env in seen]
    assert tokens == (["stale-pat"] if pat else []) + ["az-jwt", "helper-pw"], tokens
    for token, env in seen:
        config = _git_config(env)
        assert ("credential.helper", "") in config
        headers = [value for key, value in config if key == "http.extraheader" and value]
        assert headers == [f"Authorization: {expected[token]}"]
        for name in ("GIT_TOKEN", "ADO_APM_PAT", "GITHUB_TOKEN", "GITHUB_APM_PAT", "GH_TOKEN"):
            assert name not in env
        others = [cred for cred in ("stale-pat", "az-jwt", "helper-pw") if cred != token]
        text = repr(env)
        assert not any(cred in text for cred in others), (token, others)
