"""Owner tests for auth outcome classification (host-credential-resolution).

Exact ``AuthOutcome`` equality at ``apm_cli.core.auth_outcome`` so the
kind, the user-visible detail, the status and the 401 verification flag are
all pinned for every response and error class (see tests/mutation/floors.json).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from apm_cli.core.auth_outcome import (
    AuthOutcome,
    AuthOutcomeKind,
    classify_auth_outcome,
    is_ado_signin_location,
)
from apm_cli.deps.github_rate_limit import GitHubThrottle, GitHubThrottleError

pytestmark = pytest.mark.unit

K = AuthOutcomeKind
SIGNIN = "https://app.vssps.visualstudio.com/_signin?realm=x"


def _resp(status, headers=None, *, url=None, history=None) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status, headers=headers or {}, url=url, history=history or []
    )


def _hop(location: str) -> SimpleNamespace:
    return SimpleNamespace(headers={"Location": location})


@pytest.mark.parametrize(
    ("response", "kwargs", "expected"),
    [
        (_resp(200), {}, AuthOutcome(K.OK, "", 200)),
        (_resp(299, {"Content-Type": "text/html"}), {}, AuthOutcome(K.OK, "", 299)),
        (_resp(300), {}, AuthOutcome(K.OTHER, "HTTP 300 redirect", 300)),
        (_resp(399), {}, AuthOutcome(K.OTHER, "HTTP 399 redirect", 399)),
        (_resp(400), {}, AuthOutcome(K.OTHER, "HTTP 400", 400)),
        (_resp(199), {}, AuthOutcome(K.OTHER, "HTTP 199", 199)),
        (
            _resp(401),
            {"credential_presented": True},
            AuthOutcome(K.CREDENTIAL_REJECTED, "HTTP 401", 401, rejection_verified=True),
        ),
        (_resp(401), {}, AuthOutcome(K.AUTH_REQUIRED, "HTTP 401", 401)),
        (
            _resp(403),
            {"credential_presented": True},
            AuthOutcome(K.CREDENTIAL_REJECTED, "HTTP 403", 403),
        ),
        (_resp(403), {}, AuthOutcome(K.AUTH_REQUIRED, "HTTP 403", 403)),
        (_resp(404), {}, AuthOutcome(K.NOT_FOUND, "HTTP 404", 404)),
        (
            _resp(404),
            {"public_github_anonymous": True},
            AuthOutcome(K.AUTH_REQUIRED, "HTTP 404", 404),
        ),
        (_resp(429), {}, AuthOutcome(K.THROTTLED, "HTTP 429", 429)),
        (_resp(500), {}, AuthOutcome(K.OTHER, "HTTP 500", 500)),
        (_resp("500"), {}, AuthOutcome(K.OTHER, "response without status")),
        (
            _resp(302, {"Location": SIGNIN}),
            {},
            AuthOutcome(K.OTHER, "HTTP 302 redirect", 302),
        ),
    ],
)
def test_generic_host_response_classification(response, kwargs, expected) -> None:
    assert classify_auth_outcome(response=response, **kwargs) == expected


@pytest.mark.parametrize(
    ("response", "presented", "expected"),
    [
        (
            _resp(200, url=SIGNIN),
            True,
            AuthOutcome(K.CREDENTIAL_REJECTED, "Azure DevOps redirected to sign-in", 200),
        ),
        (
            _resp(200, history=[_hop("https://dev.azure.com/x"), _hop(SIGNIN)]),
            False,
            AuthOutcome(K.AUTH_REQUIRED, "Azure DevOps redirected to sign-in", 200),
        ),
        (
            _resp(200, history=[_hop(SIGNIN)]),
            True,
            AuthOutcome(K.CREDENTIAL_REJECTED, "Azure DevOps redirected to sign-in", 200),
        ),
        (
            _resp(200, history=[_hop("https://dev.azure.com/x")]),
            False,
            AuthOutcome(K.OK, "", 200),
        ),
        (
            _resp(203),
            True,
            AuthOutcome(K.CREDENTIAL_REJECTED, "Azure DevOps answered with a sign-in page", 203),
        ),
        (
            _resp(200, {"content-type": "Text/HTML; charset=utf-8"}),
            False,
            AuthOutcome(K.AUTH_REQUIRED, "Azure DevOps answered with a sign-in page", 200),
        ),
        (
            _resp(200, {"Content-Type": "application/json"}),
            False,
            AuthOutcome(K.OK, "", 200),
        ),
        (
            _resp(302, {"Location": SIGNIN}),
            True,
            AuthOutcome(K.CREDENTIAL_REJECTED, "Azure DevOps redirected to sign-in", 302),
        ),
        (
            _resp(302, {"Location": "https://dev.azure.com/org/_git/repo"}),
            True,
            AuthOutcome(K.OTHER, "HTTP 302 redirect", 302),
        ),
        (
            _resp(401),
            True,
            AuthOutcome(K.CREDENTIAL_REJECTED, "HTTP 401", 401, rejection_verified=True),
        ),
    ],
)
def test_ado_host_response_classification(response, presented, expected) -> None:
    host_info = SimpleNamespace(kind="ado", host="dev.azure.com", port=None)

    assert (
        classify_auth_outcome(
            host_info=host_info, response=response, credential_presented=presented
        )
        == expected
    )
    assert (
        classify_auth_outcome(host_kind="ado", response=response, credential_presented=presented)
        == expected
    )


@pytest.mark.parametrize("host_kind", [None, "", 7])
def test_unknown_host_kind_uses_generic_rules(host_kind) -> None:
    host_info = SimpleNamespace(kind=host_kind)

    assert classify_auth_outcome(host_info=host_info, response=_resp(203)) == AuthOutcome(
        K.OK, "", 203
    )


def test_throttle_error_wins_and_keeps_redacted_detail() -> None:
    throttle = GitHubThrottleError(GitHubThrottle(status_code=403, signal="x"), "github.com")
    wrapped = RuntimeError("401 unauthorized")
    wrapped.__cause__ = throttle

    assert classify_auth_outcome(error=throttle, response=_resp(401)) == AuthOutcome(
        K.THROTTLED, "GitHub API throttle for github.com (HTTP 403)"
    )
    assert classify_auth_outcome(error=wrapped) == AuthOutcome(K.THROTTLED, "401 unauthorized")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (requests.exceptions.SSLError("cert bad"), AuthOutcome(K.TLS, "cert bad")),
        (requests.exceptions.ConnectionError("refused"), AuthOutcome(K.NETWORK, "refused")),
        (requests.exceptions.Timeout("slow"), AuthOutcome(K.NETWORK, "slow")),
    ],
)
def test_transport_errors_win_over_responses(error, expected) -> None:
    assert classify_auth_outcome(error=error, response=_resp(401)) == expected


def test_error_responses_are_classified_from_the_first_in_the_cause_chain() -> None:
    inner = requests.exceptions.HTTPError("inner")
    inner.response = _resp(404)
    outer = requests.exceptions.HTTPError("outer")
    outer.response = _resp(401)
    outer.__cause__ = inner

    assert classify_auth_outcome(error=outer) == AuthOutcome(K.AUTH_REQUIRED, "HTTP 401", 401)
    single = requests.exceptions.HTTPError("only")
    single.response = _resp(404)
    assert classify_auth_outcome(error=single) == AuthOutcome(K.NOT_FOUND, "HTTP 404", 404)


def test_response_without_status_falls_back_to_text() -> None:
    error = RuntimeError("fatal: Authentication failed for repo")
    error.response = SimpleNamespace(headers={})

    outcome = classify_auth_outcome(error=error, credential_presented=True)

    assert outcome.kind is K.CREDENTIAL_REJECTED


def test_other_response_with_error_text_defers_to_text_rules() -> None:
    error = RuntimeError("fatal: could not read Username: terminal prompts disabled")

    outcome = classify_auth_outcome(error=error, response=_resp(500))

    assert outcome.kind is K.AUTH_REQUIRED
    assert classify_auth_outcome(error=" ", response=_resp(500)) == AuthOutcome(
        K.OTHER, "HTTP 500", 500
    )


def test_no_response_and_no_error_is_ok() -> None:
    assert classify_auth_outcome() == AuthOutcome(K.OK)
    assert classify_auth_outcome(response=SimpleNamespace(headers={})) == AuthOutcome(K.OK)


def test_public_github_anonymous_reaches_text_rules() -> None:
    error = RuntimeError("HTTP 404 Not Found")

    anonymous = classify_auth_outcome(error=error, public_github_anonymous=True)
    named = classify_auth_outcome(error=error)

    assert anonymous.kind is K.AUTH_REQUIRED
    assert named.kind is K.NOT_FOUND


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (None, False),
        ("", False),
        ("https://app.vssps.visualstudio.com/x", True),
        ("https://APP.VSSPS.DEV.AZURE.COM/x", True),
        ("https://login.microsoftonline.com/common", True),
        ("https://contoso.vssps.visualstudio.com/x", True),
        ("https://vssps.visualstudio.com.evil.example/x", False),
        ("https://dev.azure.com/org/_SignIn", True),
        ("/_signin?x=1", True),
        ("https://dev.azure.com/org/_git/repo", False),
    ],
)
def test_is_ado_signin_location(location, expected) -> None:
    assert is_ado_signin_location(location) is expected


def test_minimal_response_objects_are_read_defensively() -> None:
    bare = SimpleNamespace(status_code=200)
    hopless = SimpleNamespace(status_code=200, headers={}, url=None, history=[SimpleNamespace()])

    assert classify_auth_outcome(response=bare) == AuthOutcome(K.OK, "", 200)
    assert classify_auth_outcome(host_kind="ado", response=bare) == AuthOutcome(K.OK, "", 200)
    assert classify_auth_outcome(host_kind="ado", response=hopless) == AuthOutcome(K.OK, "", 200)
