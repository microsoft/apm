"""Unit tests for lifecycle script executors (Copilot CLI aligned)."""

from __future__ import annotations

import json
import subprocess
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


def _make_event(event_name: str = "post-install") -> LifecycleEvent:
    return LifecycleEvent(
        event=event_name,
        packages=[PackageInfo(name="org/repo", reference="v1")],
        scope="project",
        timestamp="2026-01-01T00:00:00Z",
        cli_version="0.0.0",
        working_directory="/tmp/test",
    )


class _FakePopen:
    """Minimal subprocess.Popen stand-in for command-executor tests.

    Command scripts run via Popen + communicate (with start_new_session
    so a timeout can killpg the whole group). This records the Popen
    kwargs and the communicate(input=, timeout=) call for assertions.
    """

    def __init__(
        self,
        *args,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        communicate_exc: BaseException | None = None,
        **kwargs,
    ) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._exc = communicate_exc
        self.communicate_calls: list[dict] = []
        # A stale-but-plausible pid: _kill_process_group's killpg raises
        # ProcessLookupError (caught), so no real process is signalled.
        self.pid = 2_147_483_640

    def communicate(self, input=None, timeout=None):
        self.communicate_calls.append({"input": input, "timeout": timeout})
        if self._exc is not None and len(self.communicate_calls) == 1:
            raise self._exc
        return (self._stdout, self._stderr)

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        pass


def _patch_popen(**fake_kwargs):
    """Patch subprocess.Popen to return a single _FakePopen instance.

    Uses a side_effect factory so the fake records the ACTUAL runtime
    Popen kwargs (shell, env, cwd, start_new_session) for assertions.
    """
    fake = _FakePopen(**fake_kwargs)

    def _factory(*args, **kwargs):
        fake.init_args = args
        fake.init_kwargs = kwargs
        return fake

    patcher = patch("apm_cli.core.script_executors.subprocess.Popen", side_effect=_factory)
    return patcher, fake


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
    def test_runs_command_with_stdin_payload(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="echo done")
        event = _make_event()
        patcher, fake = _patch_popen()
        with patcher:
            _execute_command(script, event)
        assert len(fake.communicate_calls) == 1
        input_data = fake.communicate_calls[0]["input"]
        assert input_data is not None
        payload = json.loads(input_data)
        assert payload["event"] == "post-install"

    def test_uses_shell_true(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="echo")
        patcher, fake = _patch_popen()
        with patcher:
            _execute_command(script, _make_event())
        assert fake.init_kwargs.get("shell") is True

    def test_uses_start_new_session(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="echo")
        patcher, fake = _patch_popen()
        with patcher:
            _execute_command(script, _make_event())
        assert fake.init_kwargs.get("start_new_session") is True

    def test_timeout_from_script(self) -> None:
        script = ScriptEntry(
            script_type="command", event="post-install", bash="sleep", timeout_sec=5
        )
        patcher, fake = _patch_popen()
        with patcher:
            _execute_command(script, _make_event())
        assert fake.communicate_calls[0]["timeout"] == 5

    def test_default_timeout(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="echo")
        patcher, fake = _patch_popen()
        with patcher:
            _execute_command(script, _make_event())
        assert fake.communicate_calls[0]["timeout"] == 30

    def test_swallows_timeout_error(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="sleep")
        patcher, _ = _patch_popen(communicate_exc=subprocess.TimeoutExpired("sleep", 30))
        with patcher:
            _execute_command(script, _make_event())

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

    def test_verbose_logs_on_timeout(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="slow")
        logger = MagicMock()
        patcher, _ = _patch_popen(communicate_exc=subprocess.TimeoutExpired("slow", 30))
        with patcher:
            _execute_command(script, _make_event(), logger=logger, verbose=True)
        # Timeout warning always emits (not verbose-gated); uses logger.warning.
        logger.warning.assert_called_once()
        assert "[!]" in logger.warning.call_args[0][0]

    def test_merges_script_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXISTING_VAR", "original")
        script = ScriptEntry(
            script_type="command",
            event="post-install",
            bash="echo",
            env={"EXTRA": "added"},
        )
        patcher, fake = _patch_popen()
        with patcher:
            _execute_command(script, _make_event())
        env = fake.init_kwargs.get("env", {})
        assert env.get("EXISTING_VAR") == "original"
        assert env.get("EXTRA") == "added"

    def test_uses_project_root_as_cwd(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="echo")
        patcher, fake = _patch_popen()
        with patcher:
            _execute_command(script, _make_event(), project_root="/my/project")
        assert fake.init_kwargs.get("cwd") == "/my/project"


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
        script = ScriptEntry(script_type="command", event="post-install", cwd="/absolute/path")
        assert _resolve_cwd(script, "/my/project") == "/absolute/path"

    def test_relative_cwd_resolved_against_project_root(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", cwd="scripts")
        result = _resolve_cwd(script, "/my/project")
        assert result == "/my/project/scripts"

    def test_traversal_outside_project_root_uses_project_root(self, tmp_path: Path) -> None:
        """cwd values that escape project_root via .. are clamped to project_root.

        This prevents a lifecycle entry like 'cwd: ../../.ssh' from redirecting
        command execution outside the project directory.
        """
        project = tmp_path / "project"
        project.mkdir()
        script = ScriptEntry(script_type="command", event="post-install", cwd="../../etc")
        result = _resolve_cwd(script, str(project))
        # The raw traversal would resolve to tmp_path.parent / "etc", which is
        # outside the project.  Containment must clamp it to the project root.
        would_escape = str((project / "../../etc").resolve())
        assert result != would_escape
        assert result == str(project.resolve())


# -- Script output log -----------------------------------------------------


class TestGetScriptsLogPath:
    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APM_HOME", raising=False)
        path = _get_scripts_log_path()
        assert path.name == "scripts.log"
        assert "logs" in path.parts

    def test_respects_apm_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", "/custom/apm")
        path = _get_scripts_log_path()
        assert str(path) == "/custom/apm/logs/scripts.log"


class TestAppendToScriptLog:
    def test_creates_log_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        _append_to_script_log("post-install", "command", "echo hi", stdout="hello world")
        log = tmp_path / "logs" / "scripts.log"
        assert log.exists()
        content = log.read_text()
        assert "post-install" in content
        assert "command" in content
        assert "echo hi" in content
        assert "hello world" in content

    def test_includes_exit_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        _append_to_script_log("pre-install", "command", "false", exit_code=1, status="error")
        content = (tmp_path / "logs" / "scripts.log").read_text()
        assert "exit_code=1" in content
        assert "status=error" in content

    def test_includes_stderr(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        _append_to_script_log("post-install", "command", "bad", stderr="not found")
        content = (tmp_path / "logs" / "scripts.log").read_text()
        assert "stderr: not found" in content

    def test_appends_multiple_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        _append_to_script_log("pre-install", "command", "echo 1")
        _append_to_script_log("post-install", "command", "echo 2")
        content = (tmp_path / "logs" / "scripts.log").read_text()
        assert "pre-install" in content
        assert "post-install" in content

    def test_swallows_write_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", "/nonexistent/readonly/path")
        # Should not raise
        _append_to_script_log("post-install", "command", "echo", stdout="hi")


class TestCommandExecutorLogging:
    def test_logs_successful_command_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        script = ScriptEntry(script_type="command", event="post-install", bash="echo done")
        patcher, _ = _patch_popen(stdout="script output line", stderr="", returncode=0)
        with patcher:
            _execute_command(script, _make_event())
        content = (tmp_path / "logs" / "scripts.log").read_text()
        assert "script output line" in content
        assert "exit_code=0" in content
        assert "status=ok" in content

    def test_logs_failed_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        script = ScriptEntry(script_type="command", event="post-install", bash="false")
        patcher, _ = _patch_popen(stdout="", stderr="something broke", returncode=1)
        with patcher:
            _execute_command(script, _make_event())
        content = (tmp_path / "logs" / "scripts.log").read_text()
        assert "something broke" in content
        assert "exit_code=1" in content
        assert "status=error" in content

    def test_logs_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        script = ScriptEntry(script_type="command", event="post-install", bash="sleep 999")
        patcher, _ = _patch_popen(communicate_exc=subprocess.TimeoutExpired("sleep", 30))
        with patcher:
            _execute_command(script, _make_event())
        content = (tmp_path / "logs" / "scripts.log").read_text()
        assert "status=timeout" in content


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
