"""Org-policy 404s are fetched once per command run (#4782).

An org without a policy repository answers every candidate with 404, and one
``apm install`` discovers policy about six times. Inside one invocation scope the
GitHub Contents 404 is reused; anything else (success, errors, timeouts) is
fetched exactly as before, and nothing outlives the scope.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.policy.discovery import discover_policy

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-policy-outcome-routing")]

_SOURCE = "contoso/.github"


class _FakePolicyServer:
    """Answers policy GETs by status and records every request it receives."""

    def __init__(self, status: int = 404, exc: Exception | None = None) -> None:
        self.status = status
        self.exc = exc
        self.urls: list[str] = []

    def __call__(self, method: str, url: str, **_kwargs: object) -> requests.Response:
        self.urls.append(url)
        if self.exc is not None:
            raise self.exc
        response = requests.Response()
        response.status_code = self.status
        response.url = url
        if self.status == 200:
            response._content = (
                b'{"content": "name: org\\nversion: \\"1.0\\"\\n'
                b'dependencies:\\n  deny: [\\"evil/*\\"]\\n"}'
            )
        else:
            response._content = b'{"message": "Not Found"}'
        return response


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakePolicyServer:
    fake = _FakePolicyServer()
    monkeypatch.setattr("apm_cli.policy._policy_http.unfollowed_redirect_request", fake)
    # An explicit token keeps the auth chain from spawning the gh CLI.
    monkeypatch.setenv("GITHUB_APM_PAT", "test-token")
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    return fake


def _discover(root: Path) -> str:
    return discover_policy(root, policy_override=_SOURCE, no_cache=True).outcome


def test_absent_policy_is_fetched_once_per_invocation(server, tmp_path):
    with invocation_scope():
        first = _discover(tmp_path)
        fetched = len(server.urls)
        outcomes = [first, *(_discover(tmp_path) for _ in range(5))]

    assert outcomes == ["absent"] * 6
    assert fetched >= 1
    assert len(server.urls) == fetched


def test_outside_an_invocation_every_discovery_fetches(server, tmp_path):
    first = _discover(tmp_path)
    fetched = len(server.urls)

    assert [first, _discover(tmp_path)] == ["absent", "absent"]
    assert len(server.urls) == 2 * fetched


def test_a_policy_published_between_runs_is_found_by_the_next_run(server, tmp_path):
    with invocation_scope():
        assert _discover(tmp_path) == "absent"

    server.status = 200
    with invocation_scope():
        assert _discover(tmp_path) == "found"


def test_errors_are_never_reused(server, tmp_path):
    server.exc = requests.exceptions.Timeout("slow")
    with invocation_scope():
        first = _discover(tmp_path)
        fetched = len(server.urls)
        second = _discover(tmp_path)

    assert first == second
    assert first != "absent"
    assert len(server.urls) == 2 * fetched


def test_a_404_is_not_reused_for_another_repository(server, tmp_path):
    with invocation_scope():
        assert _discover(tmp_path) == "absent"
        fetched = len(server.urls)
        other = discover_policy(tmp_path, policy_override="fabrikam/.github", no_cache=True)

    assert other.outcome == "absent"
    assert len(server.urls) == 2 * fetched
    paths = [urlsplit(url).path for url in server.urls[fetched:]]
    assert paths
    assert all(path.startswith("/repos/fabrikam/.github/contents/") for path in paths)


def test_a_404_is_not_reused_for_another_host(server, tmp_path):
    with invocation_scope():
        assert _discover(tmp_path) == "absent"
        fetched = len(server.urls)
        other = discover_policy(
            tmp_path, policy_override="ghe.example.com/contoso/.github", no_cache=True
        )

    assert other.outcome == "absent"
    hosts = [urlsplit(url).hostname for url in server.urls[fetched:]]
    assert hosts
    assert all(host == "ghe.example.com" for host in hosts)
