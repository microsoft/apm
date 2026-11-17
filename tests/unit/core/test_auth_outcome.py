"""Contracts of the auth-outcome classifier and the Azure DevOps chain owner."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

import apm_cli.core.token_manager as token_manager_module
from apm_cli.core.auth import AuthResolver
from apm_cli.core.auth_chain import (
    AdoAuthChainExhaustedError,
    AuthAttemptLog,
    ado_helper_path,
    authorization_headers,
)
from apm_cli.core.auth_outcome import AuthOutcomeKind, classify_auth_outcome
from apm_cli.core.token_manager import GitHubTokenManager

pytestmark = pytest.mark.component


def _response(status: int, headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, headers=headers or {}, history=[])


@pytest.mark.parametrize(
    ("response", "presented", "expected"),
    [
        (_response(401), True, AuthOutcomeKind.CREDENTIAL_REJECTED),
        (_response(401), False, AuthOutcomeKind.AUTH_REQUIRED),
        (_response(403), True, AuthOutcomeKind.CREDENTIAL_REJECTED),
        (
            _response(302, {"Location": "https://app.vssps.visualstudio.com/_signin?x=1"}),
            False,
            AuthOutcomeKind.AUTH_REQUIRED,
        ),
        (
            _response(302, {"Location": "https://login.microsoftonline.com/common/oauth2"}),
            True,
            AuthOutcomeKind.CREDENTIAL_REJECTED,
        ),
        (
            _response(302, {"Location": "https://elsewhere.example/path"}),
            True,
            AuthOutcomeKind.OTHER,
        ),
        (_response(200, {"Content-Type": "text/html"}), False, AuthOutcomeKind.AUTH_REQUIRED),
        (
            _response(203, {"Content-Type": "application/json"}),
            True,
            AuthOutcomeKind.CREDENTIAL_REJECTED,
        ),
        (_response(200, {"Content-Type": "application/json"}), True, AuthOutcomeKind.OK),
        (_response(404), True, AuthOutcomeKind.NOT_FOUND),
        (_response(429), True, AuthOutcomeKind.THROTTLED),
    ],
)
def test_ado_response_classification(response, presented: bool, expected) -> None:
    outcome = classify_auth_outcome(
        host_kind="ado", response=response, credential_presented=presented
    )
    assert outcome.kind is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("fatal: could not read Username: terminal prompts disabled", True),
        ("fatal: Authentication failed for 'https://dev.azure.com/o/p/_git/r/'", True),
        ("The requested URL returned error: 403", True),
        ("remote: TF401019: The Git repository does not exist", False),
        ("fatal: unable to access: Could not resolve host: dev.azure.com", False),
        ("SSL certificate problem: unable to get local issuer certificate", False),
    ],
)
def test_ado_git_stderr_classification(error: str, expected: bool) -> None:
    outcome = classify_auth_outcome(host_kind="ado", error=error, credential_presented=True)
    assert outcome.is_auth_failure is expected


@pytest.mark.parametrize(
    ("kwargs", "verified"),
    [
        ({"response": _response(401)}, True),
        ({"error": "fatal: could not read Username: terminal prompts disabled"}, True),
        ({"error": "fatal: Authentication failed for 'https://dev.azure.com/o/'"}, True),
        ({"error": "The requested URL returned error: 401"}, True),
        ({"response": _response(403)}, False),
        ({"error": "The requested URL returned error: 403"}, False),
        ({"error": "remote: TF400813: unauthorized"}, False),
        (
            {
                "response": _response(
                    302, {"Location": "https://app.vssps.visualstudio.com/_signin"}
                )
            },
            False,
        ),
        ({"response": _response(203, {"Content-Type": "text/html"})}, False),
    ],
)
def test_only_a_401_for_a_presented_credential_verifies_rejection(kwargs, verified) -> None:
    """Only a verified rejection may erase a helper credential (#3765)."""
    presented = classify_auth_outcome(host_kind="ado", credential_presented=True, **kwargs)
    assert presented.is_auth_failure
    assert presented.is_verified_rejection is verified
    anonymous = classify_auth_outcome(host_kind="ado", credential_presented=False, **kwargs)
    assert not anonymous.is_verified_rejection


def test_transport_errors_never_escalate() -> None:
    assert (
        classify_auth_outcome(host_kind="ado", error=requests.exceptions.ConnectionError("x")).kind
        is AuthOutcomeKind.NETWORK
    )
    assert (
        classify_auth_outcome(host_kind="ado", error=requests.exceptions.SSLError("x")).kind
        is AuthOutcomeKind.TLS
    )


def test_html_page_is_ok_outside_azure_devops() -> None:
    outcome = classify_auth_outcome(
        host_kind="github", response=_response(200, {"Content-Type": "text/html"})
    )
    assert outcome.kind is AuthOutcomeKind.OK


def test_public_github_anonymous_404_may_hide_a_private_repository() -> None:
    assert classify_auth_outcome(
        host_kind="github", response=_response(404), public_github_anonymous=True
    ).is_auth_failure
    assert not classify_auth_outcome(host_kind="github", response=_response(404)).is_auth_failure


def test_authorization_headers_use_last_active_extraheader() -> None:
    env = {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic stale",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
        "GIT_CONFIG_KEY_2": "http.extraheader",
        "GIT_CONFIG_VALUE_2": "Authorization: Bearer fresh",
        "GIT_CONFIG_KEY_3": "http.extraheader",
        "GIT_CONFIG_VALUE_3": "Authorization: Basic beyond-count",
    }
    assert authorization_headers(env) == {"Authorization": "Bearer fresh"}
    assert authorization_headers({**env, "GIT_CONFIG_COUNT": "x"}) == {}
    assert (
        authorization_headers({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader"})
        == {}
    )


def test_ado_helper_path_matches_the_git_remote_path() -> None:
    assert ado_helper_path("org/proj/repo") == "org/proj/_git/repo"
    assert ado_helper_path("org/proj/_git/repo") == "org/proj/_git/repo"
    assert ado_helper_path(None) is None


@pytest.mark.parametrize("path", ["contoso/Platform/tools", "contoso/Platform/_git/tools"])
def test_legacy_visualstudio_helper_path_drops_the_host_org(path: str) -> None:
    """Git stores ``Platform/_git/tools`` for https://contoso.visualstudio.com/Platform/_git/tools."""
    assert ado_helper_path(path, "contoso.visualstudio.com") == "Platform/_git/tools"


def test_tenant_lookup_runs_once_under_concurrency(monkeypatch) -> None:
    """Parallel diagnostics share one ``az account show`` call."""
    import threading
    import time

    from apm_cli.core.azure_cli import AzureCliBearerProvider

    provider = AzureCliBearerProvider(az_command="/fixture/az")
    calls: list[int] = []

    def _slow_lookup() -> str:
        calls.append(1)
        time.sleep(0.05)
        return "tenant-0000"

    monkeypatch.setattr(provider, "_lookup_tenant_id", _slow_lookup)
    barrier = threading.Barrier(6)
    results: list[str | None] = []

    def _worker() -> None:
        barrier.wait()
        results.append(provider.get_current_tenant_id())

    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results == ["tenant-0000"] * 6
    assert len(calls) == 1


def test_helper_credential_keeps_username_and_request_scope(monkeypatch) -> None:
    def _fill(args, *_a, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout="username=bob\npassword=secret-value\n", stderr=""
        )

    monkeypatch.setattr(token_manager_module.subprocess, "run", _fill)
    credential = GitHubTokenManager.resolve_git_helper_credential(
        "dev.azure.com", path="/org/proj/_git/repo"
    )
    assert credential is not None
    assert (credential.username, credential.host, credential.path) == (
        "bob",
        "dev.azure.com",
        "org/proj/_git/repo",
    )
    assert "secret-value" not in repr(credential)
    assert credential.description == (("username", "bob"), ("password", "secret-value"))
    assert "secret-value" not in repr(credential)


def test_ado_server_chain_skips_azure_cli(monkeypatch) -> None:
    monkeypatch.setenv("ADO_HOST", "ado.example.com")
    monkeypatch.setenv("ADO_APM_PAT", "stale")
    monkeypatch.setattr(
        "apm_cli.core.azure_cli.get_bearer_provider",
        lambda: pytest.fail("Azure DevOps Server must not consult az"),
    )
    monkeypatch.setattr(
        GitHubTokenManager, "resolve_git_helper_credential", staticmethod(lambda *a, **k: None)
    )
    attempts = AuthAttemptLog()

    def _rejected(_token, _env):
        raise RuntimeError("HTTP 401 Unauthorized")

    with pytest.raises(AdoAuthChainExhaustedError) as raised:
        AuthResolver().try_with_fallback(
            "ado.example.com", _rejected, path="coll/proj/repo", attempts=attempts
        )

    assert [a.step for a in attempts.snapshot()] == ["ADO_APM_PAT", "git credential fill"]
    assert "az CLI bearer" not in str(raised.value)
    assert raised.value.attempts is attempts


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MagicMock(), AuthOutcomeKind.OTHER),
        (b"fatal: Authentication failed", AuthOutcomeKind.AUTH_REQUIRED),
    ],
    ids=["mock", "bytes"],
)
def test_classifier_never_raises_on_foreign_error_objects(error, expected) -> None:
    """Classification reads non-exception errors as text instead of raising."""
    outcome = classify_auth_outcome(host_info=MagicMock(), error=error)
    assert outcome.kind is expected
