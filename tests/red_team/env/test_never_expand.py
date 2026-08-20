"""Vector 2: _NEVER_EXPAND must win over allowedEnvVars.

A script author can opt credential-named variables back in via
``allowedEnvVars`` -- but the four APM auth variables in ``_NEVER_EXPAND``
(``GITHUB_APM_PAT``, ``GITHUB_TOKEN``, ``GH_TOKEN``, ``ADO_APM_PAT``) must
stay blocked regardless. These are defended-behaviour regression traps:
they pass today and must keep passing.
"""

from __future__ import annotations

import pytest

from apm_cli.core.lifecycle_scripts import ScriptEntry
from apm_cli.core.script_executors import _build_script_env, _expand_env_vars

from .fixtures.helpers import capture_http_headers

NEVER_EXPAND = ["GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN", "ADO_APM_PAT"]


class TestNeverExpandHeader:
    @pytest.mark.parametrize("name", NEVER_EXPAND)
    def test_allowlist_cannot_unblock_header_expansion(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, "ghp_supersecret_value")
        allowed = frozenset({name})
        assert _expand_env_vars(f"Bearer ${{{name}}}", allowed) == "Bearer "

    @pytest.mark.parametrize("name", NEVER_EXPAND)
    def test_allowlist_cannot_unblock_header_end_to_end(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, "ghp_supersecret_value")
        script = ScriptEntry(
            script_type="http",
            event="post-install",
            url="https://collector.example.com/hook",
            headers={"Authorization": f"Bearer ${{{name}}}"},
            allowed_env_vars=[name],
        )
        headers = capture_http_headers(script, monkeypatch)
        assert "ghp_supersecret_value" not in headers.get("Authorization", "")


class TestNeverExpandCommandEnv:
    @pytest.mark.parametrize("name", NEVER_EXPAND)
    def test_allowlist_cannot_unblock_command_env(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, "ghp_supersecret_value")
        script = ScriptEntry(
            script_type="command",
            event="post-install",
            bash="env",
            allowed_env_vars=[name],
        )
        env = _build_script_env(script)
        assert name not in env, f"{name} reached subprocess env despite _NEVER_EXPAND"
