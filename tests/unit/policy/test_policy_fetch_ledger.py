"""Every org-policy fetch channel notes its attempt before its credential lookup (#5075).

The verified-state record lets a GitLab or Azure DevOps project replay a no-op
only when its run made no fetch attempt, so an attempt that fails at the
credential lookup or the transport must still be counted.
"""

from __future__ import annotations

import contextlib

import pytest

from apm_cli.core.policy_fetch_ledger import fetch_attempts
from apm_cli.policy import _gitlab, discovery
from apm_cli.policy._policy_http import policy_get

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I2")]


class _NoCredential(Exception):
    pass


def _refuse(*_args, **_kwargs):
    raise _NoCredential


def _github() -> None:
    discovery._fetch_github_contents("acme/.github", "apm-policy.yml")


def _ado() -> None:
    discovery._fetch_ado_contents("acme", "apm", "apm-policy", "apm-policy.yml")


def _gitlab_rest() -> None:
    class _Resolver:
        try_with_fallback = staticmethod(_refuse)

    _gitlab._authenticated_gitlab_request(
        _Resolver(),
        api_url="https://gitlab.com/api/v4/projects/acme%2Fapm-policy",
        host="gitlab.com",
        org="acme",
        port=None,
        project_path="acme/apm-policy",
    )


def _http() -> None:
    policy_get("not a url")


@pytest.mark.parametrize(
    "channel", [_github, _ado, _gitlab_rest, _http], ids=["github", "ado", "gitlab", "http"]
)
def test_a_failed_fetch_is_still_counted(monkeypatch: pytest.MonkeyPatch, channel) -> None:
    monkeypatch.setattr("apm_cli.core.auth.AuthResolver.try_with_fallback", _refuse)
    before = fetch_attempts()

    with contextlib.suppress(Exception):  # the channel's own error handling is not under test
        channel()

    assert fetch_attempts() > before
