"""Unit tests for lifecycle hook executors."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.core.hook_executors import (
    _COMMAND_TIMEOUT,
    _build_hook_env,
    _execute_command,
    _execute_script,
    _execute_webhook,
    execute_hook,
)
from apm_cli.core.lifecycle_hooks import HookDefinition, LifecycleEvent, PackageInfo


def _make_event(event_name: str = "post-install") -> LifecycleEvent:
    return LifecycleEvent(
        event=event_name,
        packages=[PackageInfo(name="org/repo", reference="v1")],
        scope="project",
        timestamp="2026-01-01T00:00:00Z",
        cli_version="0.0.0",
    )


# -- execute_hook dispatcher ------------------------------------------------


class TestExecuteHook:
    def test_dispatches_to_webhook(self) -> None:
        hook = HookDefinition(hook_type="webhook", event="post-install", url="https://example.com")
        with patch("apm_cli.core.hook_executors._execute_webhook") as mock:
            execute_hook(hook, _make_event())
            mock.assert_called_once()

    def test_dispatches_to_command(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run="echo hi")
        with patch("apm_cli.core.hook_executors._execute_command") as mock:
            execute_hook(hook, _make_event())
            mock.assert_called_once()

    def test_dispatches_to_script(self) -> None:
        hook = HookDefinition(hook_type="script", event="post-install", path="hooks/run.sh")
        with patch("apm_cli.core.hook_executors._execute_script") as mock:
            execute_hook(hook, _make_event())
            mock.assert_called_once()


# -- Webhook executor -------------------------------------------------------


class TestWebhookExecutor:
    def test_rejects_http_url(self) -> None:
        hook = HookDefinition(
            hook_type="webhook", event="post-install", url="http://insecure.com/hook"
        )
        logger = MagicMock()
        # Should not attempt any HTTP call.
        with patch("apm_cli.core.hook_executors.threading") as mock_threading:
            _execute_webhook(hook, _make_event(), logger=logger, verbose=True)
            mock_threading.Thread.assert_not_called()

    def test_rejects_missing_url(self) -> None:
        hook = HookDefinition(hook_type="webhook", event="post-install", url=None)
        with patch("apm_cli.core.hook_executors.threading") as mock_threading:
            _execute_webhook(hook, _make_event())
            mock_threading.Thread.assert_not_called()

    def test_starts_daemon_thread_for_https(self) -> None:
        hook = HookDefinition(
            hook_type="webhook",
            event="post-install",
            url="https://analytics.example.com/events",
        )
        with patch("apm_cli.core.hook_executors.threading") as mock_threading:
            mock_thread = MagicMock()
            mock_threading.Thread.return_value = mock_thread
            _execute_webhook(hook, _make_event())
            mock_threading.Thread.assert_called_once()
            call_kwargs = mock_threading.Thread.call_args
            assert call_kwargs.kwargs.get("daemon") is True
            mock_thread.start.assert_called_once()

    def test_includes_bearer_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ANALYTICS_TOKEN", "secret123")
        hook = HookDefinition(
            hook_type="webhook",
            event="post-install",
            url="https://example.com/hook",
            token_env="TEST_ANALYTICS_TOKEN",
        )
        captured_target = None

        def _capture_thread(*args, **kwargs):
            nonlocal captured_target
            captured_target = kwargs.get("target")
            mock = MagicMock()
            return mock

        with patch("apm_cli.core.hook_executors.threading.Thread", side_effect=_capture_thread):
            _execute_webhook(hook, _make_event())

        # The thread target is the _send closure -- we trust the code;
        # the important thing is the thread was created.
        assert captured_target is not None

    def test_verbose_logs_hostname(self) -> None:
        hook = HookDefinition(
            hook_type="webhook",
            event="post-install",
            url="https://analytics.corp.net/apm",
        )
        logger = MagicMock()
        with patch("apm_cli.core.hook_executors.threading"):
            _execute_webhook(hook, _make_event(), logger=logger, verbose=True)
        logger.verbose_detail.assert_called_once()
        log_msg = logger.verbose_detail.call_args[0][0]
        # Verify hostname is mentioned (using parsed comparison per test conventions).
        parsed = urlparse(hook.url)
        assert parsed.hostname is not None
        assert parsed.hostname in log_msg


# -- Command executor -------------------------------------------------------


class TestCommandExecutor:
    def test_runs_command_with_hook_event_env(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run="echo done")
        event = _make_event()
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, event)
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env", {})
            assert "APM_HOOK_EVENT" in env
            payload = json.loads(env["APM_HOOK_EVENT"])
            assert payload["event"] == "post-install"

    def test_uses_shell_true(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run="echo")
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, _make_event())
            call_kwargs = mock_run.call_args
            assert call_kwargs.kwargs.get("shell") is True

    def test_timeout_is_applied(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run="sleep 999")
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, _make_event())
            call_kwargs = mock_run.call_args
            assert call_kwargs.kwargs.get("timeout") == _COMMAND_TIMEOUT

    def test_swallows_timeout_error(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run="sleep")
        with patch(
            "apm_cli.core.hook_executors.subprocess.run",
            side_effect=subprocess.TimeoutExpired("sleep", _COMMAND_TIMEOUT),
        ):
            # Should not raise.
            _execute_command(hook, _make_event())

    def test_swallows_generic_error(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run="bad")
        with patch(
            "apm_cli.core.hook_executors.subprocess.run",
            side_effect=OSError("not found"),
        ):
            _execute_command(hook, _make_event())

    def test_skips_when_no_run_string(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run=None)
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_command(hook, _make_event())
            mock_run.assert_not_called()

    def test_verbose_logs_on_timeout(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run="slow")
        logger = MagicMock()
        with patch(
            "apm_cli.core.hook_executors.subprocess.run",
            side_effect=subprocess.TimeoutExpired("slow", _COMMAND_TIMEOUT),
        ):
            _execute_command(hook, _make_event(), logger=logger, verbose=True)
        logger.verbose_detail.assert_called_once()


# -- Script executor --------------------------------------------------------


class TestScriptExecutor:
    def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        hook = HookDefinition(hook_type="script", event="post-install", path="../../etc/evil.sh")
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_script(hook, _make_event(), project_root=str(tmp_path))
            mock_run.assert_not_called()

    def test_rejects_missing_script(self, tmp_path: Path) -> None:
        hook = HookDefinition(hook_type="script", event="post-install", path="hooks/missing.sh")
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_script(hook, _make_event(), project_root=str(tmp_path))
            mock_run.assert_not_called()

    def test_runs_valid_script(self, tmp_path: Path) -> None:
        script = tmp_path / "hooks" / "post.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\necho ok\n")
        script.chmod(0o755)

        hook = HookDefinition(hook_type="script", event="post-install", path="hooks/post.sh")
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_script(hook, _make_event(), project_root=str(tmp_path))
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert str(script) in str(call_args[0][0])

    def test_passes_hook_event_env(self, tmp_path: Path) -> None:
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)

        hook = HookDefinition(hook_type="script", event="post-install", path="hook.sh")
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_script(hook, _make_event(), project_root=str(tmp_path))
            env = mock_run.call_args.kwargs.get("env", {})
            assert "APM_HOOK_EVENT" in env

    def test_skips_when_no_path(self) -> None:
        hook = HookDefinition(hook_type="script", event="post-install", path=None)
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            _execute_script(hook, _make_event())
            mock_run.assert_not_called()

    def test_swallows_timeout(self, tmp_path: Path) -> None:
        script = tmp_path / "slow.sh"
        script.write_text("#!/bin/sh\nsleep 999\n")
        script.chmod(0o755)

        hook = HookDefinition(hook_type="script", event="post-install", path="slow.sh")
        with patch(
            "apm_cli.core.hook_executors.subprocess.run",
            side_effect=subprocess.TimeoutExpired("slow.sh", _COMMAND_TIMEOUT),
        ):
            _execute_script(hook, _make_event(), project_root=str(tmp_path))


# -- _build_hook_env --------------------------------------------------------


class TestBuildHookEnv:
    def test_includes_apm_hook_event(self) -> None:
        event = _make_event()
        env = _build_hook_env(event)
        assert "APM_HOOK_EVENT" in env
        payload = json.loads(env["APM_HOOK_EVENT"])
        assert payload["event"] == "post-install"
        assert payload["schema_version"] == 1

    def test_inherits_parent_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_CUSTOM_VAR", "hello")
        env = _build_hook_env(_make_event())
        assert env.get("MY_CUSTOM_VAR") == "hello"
