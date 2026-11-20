"""Unit tests for lifecycle script executors (Copilot CLI aligned)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo, ScriptEntry
from apm_cli.core.script_executors import (
    _append_to_script_log,
    _build_script_env,
    _execute_command,
    _execute_http,
    _expand_env_vars,
    _get_scripts_log_path,
    _redact_url_credentials,
    _resolve_cwd,
    execute_script,
)

pytestmark = pytest.mark.unit


def _make_event(event_name: str = "post-install") -> LifecycleEvent:
    return LifecycleEvent(
        event=event_name,
        packages=[PackageInfo(name="org/repo", reference="v1")],
        scope="project",
        timestamp="2026-01-01T00:00:00Z",
        cli_version="0.0.0",
        working_directory="/tmp/test",
    )


# -- execute_script dispatcher ---------------------------------------------


class TestExecuteScript:
    def test_dispatches_to_http(self) -> None:
        script = ScriptEntry(script_type="http", event="post-install", url="https://example.com")
        with patch("apm_cli.core.script_executors._execute_http") as mock:
            execute_script(script, _make_event())
            mock.assert_called_once()

    def test_dispatches_to_command(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="echo hi")
        with patch("apm_cli.core.script_executors._execute_command") as mock:
            execute_script(script, _make_event())
            mock.assert_called_once()


# -- HTTP executor ---------------------------------------------------------


class TestHttpExecutor:
    def test_rejects_http_url(self) -> None:
        script = ScriptEntry(
            script_type="http", event="post-install", url="http://insecure.com/script"
        )
        logger = MagicMock()
        with patch("apm_cli.core.script_executors.threading") as mock_threading:
            _execute_http(script, _make_event(), logger=logger, verbose=True)
            mock_threading.Thread.assert_not_called()

    def test_rejects_missing_url(self) -> None:
        script = ScriptEntry(script_type="http", event="post-install", url=None)
        with patch("apm_cli.core.script_executors.threading") as mock_threading:
            _execute_http(script, _make_event())
            mock_threading.Thread.assert_not_called()

    def test_starts_daemon_thread_for_https(self) -> None:
        script = ScriptEntry(
            script_type="http",
            event="post-install",
            url="https://analytics.example.com/events",
        )
        with patch("apm_cli.core.script_executors.threading") as mock_threading:
            mock_thread = MagicMock()
            mock_threading.Thread.return_value = mock_thread
            _execute_http(script, _make_event())
            mock_threading.Thread.assert_called_once()
            call_kwargs = mock_threading.Thread.call_args
            assert call_kwargs.kwargs.get("daemon") is True
            mock_thread.start.assert_called_once()

    def test_verbose_logs_hostname(self) -> None:
        script = ScriptEntry(
            script_type="http",
            event="post-install",
            url="https://analytics.corp.net/apm",
        )
        logger = MagicMock()
        with patch("apm_cli.core.script_executors.threading"):
            _execute_http(script, _make_event(), logger=logger, verbose=True)
        logger.verbose_detail.assert_called_once()
        log_msg = logger.verbose_detail.call_args[0][0]
        parsed = urlparse(script.url)
        assert parsed.hostname is not None
        assert parsed.hostname in log_msg


# -- Command executor ------------------------------------------------------


class TestCommandExecutor:
    def test_swallows_generic_error(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="bad")
        with patch(
            "apm_cli.core.script_executors.subprocess.Popen",
            side_effect=OSError("not found"),
        ):
            _execute_command(script, _make_event())

    def test_skips_when_no_command(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install")
        with patch("apm_cli.core.script_executors.subprocess.Popen") as mock_popen:
            _execute_command(script, _make_event())
            mock_popen.assert_not_called()


# -- _expand_env_vars ------------------------------------------------------


class TestExpandEnvVars:
    def test_expands_dollar_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_HOST", "example.com")
        assert _expand_env_vars("Host: $MY_HOST") == "Host: example.com"

    def test_expands_braced_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_HOST", "example.com")
        assert _expand_env_vars("Host: ${MY_HOST}") == "Host: example.com"

    def test_missing_var_becomes_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NONEXISTENT", raising=False)
        assert _expand_env_vars("Bearer $NONEXISTENT") == "Bearer "

    def test_no_vars_unchanged(self) -> None:
        assert _expand_env_vars("plain text") == "plain text"

    def test_blocks_github_apm_pat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_APM_PAT", "ghp_secret123")
        assert _expand_env_vars("Bearer ${GITHUB_APM_PAT}") == "Bearer "

    def test_blocks_ado_apm_pat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADO_APM_PAT", "ado_secret")
        assert _expand_env_vars("$ADO_APM_PAT") == ""

    def test_blocks_token_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_tok")
        assert _expand_env_vars("${GITHUB_TOKEN}") == ""

    def test_blocks_secret_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SECRET", "s3cr3t")
        assert _expand_env_vars("$MY_SECRET") == ""

    def test_blocks_password_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_PASSWORD", "pass123")
        assert _expand_env_vars("${DB_PASSWORD}") == ""

    def test_blocks_key_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_KEY", "key123")
        assert _expand_env_vars("${API_KEY}") == ""

    def test_allows_safe_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_HEADER_VALUE", "safe-value")
        assert _expand_env_vars("${MY_HEADER_VALUE}") == "safe-value"


# -- _build_script_env -----------------------------------------------------


class TestBuildScriptEnv:
    def test_inherits_safe_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_CUSTOM_VAR", "hello")
        script = ScriptEntry(script_type="command", event="post-install")
        env = _build_script_env(script)
        assert env.get("MY_CUSTOM_VAR") == "hello"

    def test_merges_script_env(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", env={"FOO": "bar"})
        env = _build_script_env(script)
        assert env.get("FOO") == "bar"

    def test_strips_credential_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_APM_PAT", "ghp_secret")
        monkeypatch.setenv("ADO_APM_PAT", "ado_secret")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_tok")
        monkeypatch.setenv("MY_SECRET", "s3cr3t")
        monkeypatch.setenv("DB_PASSWORD", "pass")
        monkeypatch.setenv("API_KEY", "key")
        monkeypatch.setenv("SAFE_VAR", "kept")
        script = ScriptEntry(script_type="command", event="post-install")
        env = _build_script_env(script)
        assert "GITHUB_APM_PAT" not in env
        assert "ADO_APM_PAT" not in env
        assert "GITHUB_TOKEN" not in env
        assert "MY_SECRET" not in env
        assert "DB_PASSWORD" not in env
        assert "API_KEY" not in env
        assert env.get("SAFE_VAR") == "kept"

    def test_preserves_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        script = ScriptEntry(script_type="command", event="post-install")
        env = _build_script_env(script)
        assert env.get("PATH") == "/usr/bin:/bin"


# -- _resolve_cwd ----------------------------------------------------------


class TestResolveCwd:
    def test_returns_project_root_when_no_cwd(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install")
        assert _resolve_cwd(script, "/my/project") == "/my/project"

    def test_absolute_cwd_used_directly(self) -> None:
        abs_cwd = os.path.abspath(os.path.join(os.sep, "absolute", "path"))
        script = ScriptEntry(script_type="command", event="post-install", cwd=abs_cwd)
        assert _resolve_cwd(script, "/my/project") == abs_cwd

    def test_relative_cwd_resolved_against_project_root(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", cwd="scripts")
        result = _resolve_cwd(script, "/my/project")
        assert result == str((Path("/my/project") / "scripts").resolve())


# -- Script output log -----------------------------------------------------


class TestGetScriptsLogPath:
    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APM_HOME", raising=False)
        path = _get_scripts_log_path()
        assert path.name == "scripts.log"
        assert "logs" in path.parts


class TestAppendToScriptLog:
    def test_swallows_write_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", "/nonexistent/readonly/path")
        # Should not raise
        _append_to_script_log("post-install", "command", "echo", stdout="hi")


# -- URL redaction ---------------------------------------------------------


class TestRedactUrlCredentials:
    def test_plain_url_unchanged(self) -> None:
        assert _redact_url_credentials("https://example.com/script") == "https://example.com/script"

    def test_strips_user_password(self) -> None:
        result = _redact_url_credentials("https://user:secret@example.com/script")
        assert "user" not in result
        assert "secret" not in result
        assert "example.com/script" in result

    def test_strips_user_only(self) -> None:
        result = _redact_url_credentials("https://user@example.com/script")
        assert "user" not in result
        assert "example.com/script" in result
