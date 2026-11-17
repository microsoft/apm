"""Policy fetches route through the origin-bounded HTTP owner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apm_cli.policy._policy_http import policy_get
from apm_cli.policy.discovery import _fetch_ado_contents

_SESSION_REQUEST = "apm_cli.utils.http_credentials.CredentialScopedSession.request"


def _response(status: int, location: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Location": location} if location else {}
    resp.text = "name: org-policy\n"
    return resp


def test_sign_in_redirect_is_returned_unfollowed() -> None:
    sign_in = "https://contoso.vssps.visualstudio.com/_signin"
    with patch(_SESSION_REQUEST, return_value=_response(302, sign_in)) as request:
        resp = policy_get("https://dev.azure.com/contoso/p/_apis/git/items?path=x")

    assert resp.status_code == 302
    assert request.call_count == 1
    assert request.call_args.kwargs["allow_redirects"] is False


def test_anonymous_attempt_keeps_ambient_netrc_out() -> None:
    with patch(_SESSION_REQUEST, return_value=_response(200)) as request:
        policy_get("https://dev.azure.com/contoso/p/_apis/x", ambient_credentials=False)

    assert request.call_args.kwargs.get("auth") is not None
    assert "Authorization" not in request.call_args.kwargs["headers"]


def test_ado_policy_sends_only_the_chain_selected_credential() -> None:
    attempts = [
        {},
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Bearer chain-bearer",
        },
    ]
    resolver = MagicMock()

    def _chain(_host, operation, **_kwargs):
        try:
            return operation(None, attempts[0])
        except Exception:
            return operation(None, attempts[1])

    resolver.try_with_fallback.side_effect = _chain
    responses = [_response(401), _response(200)]
    with (
        patch("apm_cli.policy.discovery._policy_auth_resolver", return_value=resolver),
        patch(_SESSION_REQUEST, side_effect=responses) as request,
    ):
        content, error = _fetch_ado_contents(
            org="contoso", project="p", repo=".github", file_path="apm-policy.yml"
        )

    assert error is None and content == "name: org-policy\n"
    sent = [call.kwargs["headers"].get("Authorization") for call in request.call_args_list]
    assert sent == [None, "Bearer chain-bearer"]
    assert all(call.kwargs.get("auth") is not None for call in request.call_args_list)
