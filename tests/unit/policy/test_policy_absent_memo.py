"""Org-policy 404s are fetched once per command run (#4782).

An org without a policy repository answers every candidate with 404, and one
``apm install`` discovers policy about six times. Inside one invocation scope the
GitHub Contents 404 is reused; anything else (success, errors, timeouts) is
fetched exactly as before, and nothing outlives the scope.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from apm_cli.core.invocation_memo import (
    forget_invocation_memos,
    invocation_memo,
    invocation_scope,
    memoized_per_invocation,
)
from apm_cli.policy.discovery import discover_policy

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-policy-outcome-routing")]

_SOURCE = "contoso/.github"


class _FakePolicyServer:
    """Answers policy GETs by status and records every request it receives."""

    def __init__(self, status: int = 404, exc: Exception | None = None) -> None:
        self.status = status
        self.exc = exc
        self.urls: list[str] = []
        self.auth_headers: list[str | None] = []

    def __call__(self, method: str, url: str, **kwargs: object) -> requests.Response:
        self.urls.append(url)
        headers = kwargs.get("headers") or {}
        self.auth_headers.append(dict(headers).get("Authorization"))
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


def test_the_reused_404_is_the_authenticated_answer(server, tmp_path):
    """The memo stores the whole auth chain's outcome, never an anonymous attempt."""
    with invocation_scope():
        assert _discover(tmp_path) == "absent"
        fetched = len(server.auth_headers)
        assert _discover(tmp_path) == "absent"

    assert len(server.auth_headers) == fetched
    assert any(header and "test-token" in header for header in server.auth_headers)


def test_the_scope_is_closed_and_cleared_on_exit():
    with invocation_scope():
        invocation_memo("probe")["key"] = "value"
        assert invocation_memo("probe") == {"key": "value"}

    assert invocation_memo("probe") is None
    with invocation_scope():
        assert invocation_memo("probe") == {}
    assert invocation_memo("probe") is None


def test_each_cli_invocation_gets_a_fresh_scope_closed_at_exit(monkeypatch):
    import click
    from click.testing import CliRunner

    from apm_cli.cli import cli

    @click.command("memo-probe")
    def probe() -> None:
        memo = invocation_memo("probe")
        click.echo(f"seen={sorted(memo)}")
        memo["mark"] = True

    monkeypatch.setenv("APM_E2E_TESTS", "1")  # skips the update notice
    monkeypatch.setitem(cli.commands, "memo-probe", probe)
    results = [CliRunner().invoke(cli, ["memo-probe"], obj={}) for _ in range(2)]

    assert [result.exit_code for result in results] == [0, 0]
    assert [result.output for result in results] == ["seen=[]\n", "seen=[]\n"]
    assert invocation_memo("probe") is None


def test_overlapping_scopes_share_nothing_and_leave_nothing_behind():
    first, second = invocation_scope(), invocation_scope()
    first.__enter__()
    invocation_memo("probe")["key"] = "from-first"
    second.__enter__()
    assert invocation_memo("probe") is None
    first.__exit__(None, None, None)  # exits out of order, before the scope it overlaps
    assert invocation_memo("probe") is None
    second.__exit__(None, None, None)

    assert invocation_memo("probe") is None
    with invocation_scope():
        assert invocation_memo("probe") == {}


@pytest.mark.parametrize(("kept", "expected_calls"), [(True, 1), (False, 8)])
def test_concurrent_misses_fetch_once_and_rejected_results_retry(kept, expected_calls):
    calls: list[str] = []
    started = threading.Barrier(8)

    @memoized_per_invocation(f"concurrency-{kept}", keep=lambda _result: kept)
    def fetch(key: str) -> str:
        calls.append(key)
        time.sleep(0.05)
        return f"result-{key}"

    def worker(_index: int) -> str:
        started.wait()
        return fetch("same")

    with invocation_scope(), ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))

    assert results == ["result-same"] * 8
    assert len(calls) == expected_calls


def test_a_lifecycle_command_script_drops_memos_so_refreshed_credentials_apply(server, tmp_path):
    """A pre-install script may refresh credentials; a 404 seen before it is not reused."""
    import sys

    from apm_cli.core.lifecycle_scripts import LifecycleEvent, LifecycleScriptRunner, ScriptEntry

    script = ScriptEntry(
        script_type="command", event="pre-install", command=f'"{sys.executable}" -c "pass"'
    )
    runner = LifecycleScriptRunner(scripts=[script], project_root=str(tmp_path))
    with invocation_scope():
        assert _discover(tmp_path) == "absent"
        runner.fire("pre-install", LifecycleEvent(event="pre-install"))
        server.status = 200  # the policy became visible to the refreshed credentials
        assert _discover(tmp_path) == "found"


@pytest.mark.parametrize(
    "command", ['-c "raise SystemExit(3)"', '-c "pass"'], ids=["fails", "passes"]
)
def test_every_command_script_drops_memos_even_captured_ones(tmp_path, command):
    import sys

    from apm_cli.core.lifecycle_scripts import LifecycleEvent, LifecycleScriptRunner, ScriptEntry

    script = ScriptEntry(
        script_type="command", event="post-install", command=f'"{sys.executable}" {command}'
    )
    runner = LifecycleScriptRunner(scripts=[script], project_root=str(tmp_path))
    with invocation_scope():
        captured = invocation_memo("probe")
        captured["key"] = "before-script"
        runner.fire("post-install", LifecycleEvent(event="post-install"))

        assert captured == {}
        assert invocation_memo("probe") == {}


def test_an_in_flight_fetch_never_serves_a_value_across_an_invalidation():
    calls: list[str] = []
    entered, release = threading.Event(), threading.Event()

    @memoized_per_invocation("in-flight", keep=lambda _result: True)
    def fetch(key: str) -> str:
        calls.append(key)
        if len(calls) == 1:
            entered.set()
            release.wait(5)
            return "before-invalidation"
        return "after-invalidation"

    with invocation_scope(), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fetch, "key")
        assert entered.wait(5)
        waiter = pool.submit(fetch, "key")  # holds the old memo, waits on its key lock
        time.sleep(0.2)
        forget_invocation_memos()
        release.set()

        assert first.result(5) == "before-invalidation"
        assert waiter.result(5) == "after-invalidation"
        assert fetch("key") == "after-invalidation"
