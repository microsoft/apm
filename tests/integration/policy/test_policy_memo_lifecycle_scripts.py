"""Command scripts run mid-command drop the per-command policy memo (#4782).

A pre- or post-install command script may refresh credentials, so a 404 seen
before it must not be reused after it, whether the script passes or fails.
These tests spawn real script processes, so they live here, not in tests/unit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

from apm_cli.core.invocation_memo import invocation_memo, invocation_scope
from apm_cli.core.lifecycle_scripts import LifecycleEvent, LifecycleScriptRunner, ScriptEntry
from apm_cli.policy.discovery import discover_policy

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-outcome-routing")]

_POLICY = (
    b'{"content": "name: org\\nversion: \\"1.0\\"\\ndependencies:\\n  deny: [\\"evil/*\\"]\\n"}'
)


class _StatusServer:
    def __init__(self) -> None:
        self.status = 404

    def __call__(self, method: str, url: str, **kwargs: object) -> requests.Response:
        response = requests.Response()
        response.status_code = self.status
        response.url = url
        response._content = _POLICY if self.status == 200 else b'{"message": "Not Found"}'
        return response


def _runner(root: Path, event: str, code: str) -> LifecycleScriptRunner:
    marker = root / f"{event}.ran"
    command = f'"{sys.executable}" -c "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(\'ran\'); {code}" "{marker}"'
    script = ScriptEntry(script_type="command", event=event, command=command)
    return LifecycleScriptRunner(scripts=[script], project_root=str(root))


def test_a_pre_install_script_drops_memos_so_refreshed_credentials_apply(monkeypatch, tmp_path):
    server = _StatusServer()
    monkeypatch.setattr("apm_cli.policy._policy_http.unfollowed_redirect_request", server)
    # An explicit token keeps the auth chain from spawning the gh CLI.
    monkeypatch.setenv("GITHUB_APM_PAT", "test-token")
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)

    def discover() -> str:
        return discover_policy(tmp_path, policy_override="contoso/.github", no_cache=True).outcome

    with invocation_scope():
        assert discover() == "absent"
        _runner(tmp_path, "pre-install", "pass").fire(
            "pre-install", LifecycleEvent(event="pre-install")
        )
        server.status = 200  # the policy became visible to the refreshed credentials
        assert discover() == "found"
    assert (tmp_path / "pre-install.ran").read_text() == "ran"


@pytest.mark.parametrize("code", ["raise SystemExit(3)", "pass"], ids=["fails", "passes"])
def test_every_command_script_drops_memos_even_captured_ones(tmp_path, code):
    with invocation_scope():
        captured = invocation_memo("probe")
        captured["key"] = "before-script"
        _runner(tmp_path, "post-install", code).fire(
            "post-install", LifecycleEvent(event="post-install")
        )

        assert captured == {}
        assert invocation_memo("probe") == {}
    assert (tmp_path / "post-install.ran").read_text() == "ran"
