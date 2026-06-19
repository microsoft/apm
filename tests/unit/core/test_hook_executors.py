"""Unit tests for lifecycle hook executors (Copilot CLI aligned)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.core.hook_executors import (
    _append_to_hook_log,
    _build_hook_env,
    _execute_command,
    _execute_http,
    _expand_env_vars,
    _get_hooks_log_path,
    _resolve_cwd,
    execute_hook,
)
from apm_cli.core.lifecycle_hooks import HookEntry, LifecycleEvent, PackageInfo


def _make_event(event_name: str = "post-install") -> LifecycleEvent:
    return LifecycleEvent(
        event=event_name,
        packages=[PackageInfo(name="org/repo", reference="v1")],
        scope="project",
        timestamp="2026-01-01T00:00:00Z",
        cli_version="0.0.0",
        working_directory="/tmp/test",
    )


# -- execute_hook dispatcher ------------------------------------------------


class TestExecuteHook:
    def test_dispatches_to_http(self) -> None:
        hook = HookEntry(hook_type="http", event="post-install", url="https://example.com")
        with patch("apm_cli.core.hook_executors._execute_http") as mock:
            execute_hook(hook, _make_event())
            mock.assert_called_once()

    def test_dispatches_to_command(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="echo hi")
        with patch("apm_cli.core.hook_executors._execute_command") as mock:
            execute_hook(hook, _make_event())
            mock.assert_called_once()


# -- HTTP executor ----------------------------------------------------------


class TestHttpExecutor:
    def test_rejects_http_url(self) -> None:
        hook = HookEntry(hook_type="http", event="post-install", url="http://insecure.com/hook")
        logger = MagicMock()
        with patch("apm_cli.core.hook_executors.threading") as mock_threading:
            _execute_http(hook, _make_event(), logger=logger, verbose=True)
            mock_threading.Thread.assert_not_called()

    def test_rejects_missing_url(self) -> None:
        hook = HookEntry(hook_type="http", event="post-install", url=None)
        with patch("apm_cli.core.hook_executors.threading") as mock_threading:
            _execute_http(hook, _make_event())
            mock_threading.Thread.assert_not_called()

    def test_starts_daemon_thread_for_https(self) -> None:
        hook = HookEntry(
            hook_type="http",
            event="post-install",
            url="https://analytics.example.com/events",
        )
        with patch("apm_cli.core.hook_executors.threading") as mock_threading:
            mock_thread = MagicMock()
            mock_threading.Thread.return_value = mock_thread
            _execute_http(hook, _make_event())
            mock_threading.Thread.assert_called_once()
            call_kwargs = mock_threading.Thread.call_args
            assert call_kwargs.kwargs.get("daemon") is True
            mock_thread.start.assert_called_once()

    def test_verbose_logs_hostname(self) -> None:
        hook = HookEntry(
            hook_type="http",
            event="post-install",
            url="https://analytics.corp.net/apm",
        )
        logger = MagicMock()
        with patch("apm_cli.core.hook_executors.threading"):
            _execute_http(hook, _make_event(), logger=logger, verbose=True)
        logger.verbose_detail.assert_called_once()
        log_msg = logger.verbose_detail.call_args[0][0]
        parsed = urlparse(hook.url)
        assert parsed.hostname is not None
        assert parsed.hostname in log_msg


# -- Command executor -------------------------------------------------------


class TestCommandExecutor:
    def test_runs_command_with_stdin_payload(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="echo done")
        event = _make_event()
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, event)
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            input_data = call_kwargs.kwargs.get("input")
            assert input_data is not None
            payload = json.loads(input_data)
            assert payload["event"] == "post-install"

    def test_uses_shell_true(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="echo")
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, _make_event())
            call_kwargs = mock_run.call_args
            assert call_kwargs.kwargs.get("shell") is True

    def test_timeout_from_hook(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="sleep", timeout_sec=5)
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, _make_event())
            call_kwargs = mock_run.call_args
            assert call_kwargs.kwargs.get("timeout") == 5

    def test_default_timeout(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="echo")
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, _make_event())
            call_kwargs = mock_run.call_args
            assert call_kwargs.kwargs.get("timeout") == 30

    def test_swallows_timeout_error(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="sleep")
        with patch(
            "apm_cli.core.hook_executors.subprocess.run",
            side_effect=subprocess.TimeoutExpired("sleep", 30),
        ):
            _execute_command(hook, _make_event())

    def test_swallows_generic_error(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="bad")
        with patch(
            "apm_cli.core.hook_executors.subprocess.run",
            side_effect=OSError("not found"),
        ):
            _execute_command(hook, _make_event())

    def test_skips_when_no_command(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install")
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, _make_event())
            mock_run.assert_not_called()

    def test_verbose_logs_on_timeout(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="slow")
        logger = MagicMock()
        with patch(
            "apm_cli.core.hook_executors.subprocess.run",
            side_effect=subprocess.TimeoutExpired("slow", 30),
        ):
            _execute_command(hook, _make_event(), logger=logger, verbose=True)
        logger.verbose_detail.assert_called_once()

    def test_merges_hook_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXISTING_VAR", "original")
        hook = HookEntry(
            hook_type="command", event="post-install", bash="echo", env={"EXTRA": "added"}
        )
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, _make_event())
            env = mock_run.call_args.kwargs.get("env", {})
            assert env.get("EXISTING_VAR") == "original"
            assert env.get("EXTRA") == "added"

    def test_uses_project_root_as_cwd(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="echo")
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, _make_event(), project_root="/my/project")
            assert mock_run.call_args.kwargs.get("cwd") == "/my/project"


# -- _expand_env_vars -------------------------------------------------------


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


# -- _build_hook_env --------------------------------------------------------


class TestBuildHookEnv:
    def test_inherits_safe_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_CUSTOM_VAR", "hello")
        hook = HookEntry(hook_type="command", event="post-install")
        env = _build_hook_env(hook)
        assert env.get("MY_CUSTOM_VAR") == "hello"

    def test_merges_hook_env(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", env={"FOO": "bar"})
        env = _build_hook_env(hook)
        assert env.get("FOO") == "bar"

    def test_strips_credential_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_APM_PAT", "ghp_secret")
        monkeypatch.setenv("ADO_APM_PAT", "ado_secret")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_tok")
        monkeypatch.setenv("MY_SECRET", "s3cr3t")
        monkeypatch.setenv("DB_PASSWORD", "pass")
        monkeypatch.setenv("API_KEY", "key")
        monkeypatch.setenv("SAFE_VAR", "kept")
        hook = HookEntry(hook_type="command", event="post-install")
        env = _build_hook_env(hook)
        assert "GITHUB_APM_PAT" not in env
        assert "ADO_APM_PAT" not in env
        assert "GITHUB_TOKEN" not in env
        assert "MY_SECRET" not in env
        assert "DB_PASSWORD" not in env
        assert "API_KEY" not in env
        assert env.get("SAFE_VAR") == "kept"

    def test_preserves_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        hook = HookEntry(hook_type="command", event="post-install")
        env = _build_hook_env(hook)
        assert env.get("PATH") == "/usr/bin:/bin"


# -- _resolve_cwd -----------------------------------------------------------


class TestResolveCwd:
    def test_returns_project_root_when_no_cwd(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install")
        assert _resolve_cwd(hook, "/my/project") == "/my/project"

    def test_absolute_cwd_used_directly(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", cwd="/absolute/path")
        assert _resolve_cwd(hook, "/my/project") == "/absolute/path"

    def test_relative_cwd_resolved_against_project_root(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", cwd="scripts")
        result = _resolve_cwd(hook, "/my/project")
        assert result == "/my/project/scripts"


# -- Hook output log -------------------------------------------------------


class TestGetHooksLogPath:
    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APM_HOME", raising=False)
        path = _get_hooks_log_path()
        assert path.name == "hooks.log"
        assert "logs" in path.parts

    def test_respects_apm_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", "/custom/apm")
        path = _get_hooks_log_path()
        assert str(path) == "/custom/apm/logs/hooks.log"


class TestAppendToHookLog:
    def test_creates_log_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        _append_to_hook_log("post-install", "command", "echo hi", stdout="hello world")
        log = tmp_path / "logs" / "hooks.log"
        assert log.exists()
        content = log.read_text()
        assert "post-install" in content
        assert "command" in content
        assert "echo hi" in content
        assert "hello world" in content

    def test_includes_exit_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        _append_to_hook_log("pre-install", "command", "false", exit_code=1, status="error")
        content = (tmp_path / "logs" / "hooks.log").read_text()
        assert "exit_code=1" in content
        assert "status=error" in content

    def test_includes_stderr(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        _append_to_hook_log("post-install", "command", "bad", stderr="not found")
        content = (tmp_path / "logs" / "hooks.log").read_text()
        assert "stderr: not found" in content

    def test_appends_multiple_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        _append_to_hook_log("pre-install", "command", "echo 1")
        _append_to_hook_log("post-install", "command", "echo 2")
        content = (tmp_path / "logs" / "hooks.log").read_text()
        assert "pre-install" in content
        assert "post-install" in content

    def test_swallows_write_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", "/nonexistent/readonly/path")
        # Should not raise
        _append_to_hook_log("post-install", "command", "echo", stdout="hi")


class TestCommandExecutorLogging:
    def test_logs_successful_command_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        hook = HookEntry(hook_type="command", event="post-install", bash="echo done")
        mock_result = MagicMock()
        mock_result.stdout = "hook output line"
        mock_result.stderr = ""
        mock_result.returncode = 0
        with patch("apm_cli.core.hook_executors.subprocess.run", return_value=mock_result):
            _execute_command(hook, _make_event())
        content = (tmp_path / "logs" / "hooks.log").read_text()
        assert "hook output line" in content
        assert "exit_code=0" in content
        assert "status=ok" in content

    def test_logs_failed_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        hook = HookEntry(hook_type="command", event="post-install", bash="false")
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "something broke"
        mock_result.returncode = 1
        with patch("apm_cli.core.hook_executors.subprocess.run", return_value=mock_result):
            _execute_command(hook, _make_event())
        content = (tmp_path / "logs" / "hooks.log").read_text()
        assert "something broke" in content
        assert "exit_code=1" in content
        assert "status=error" in content

    def test_logs_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        hook = HookEntry(hook_type="command", event="post-install", bash="sleep 999")
        with patch(
            "apm_cli.core.hook_executors.subprocess.run",
            side_effect=subprocess.TimeoutExpired("sleep", 30),
        ):
            _execute_command(hook, _make_event())
        content = (tmp_path / "logs" / "hooks.log").read_text()
        assert "status=timeout" in content
