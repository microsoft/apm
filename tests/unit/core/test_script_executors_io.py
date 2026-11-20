"""Unit tests for lifecycle script executors (Copilot CLI aligned) (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo, ScriptEntry
from apm_cli.core.script_executors import (
    _append_to_script_log,
    _execute_command,
    _get_scripts_log_path,
    _resolve_cwd,
)

pytestmark = pytest.mark.component


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

    patcher = patch("subprocess.Popen", side_effect=_factory)
    return patcher, fake


@contextlib.contextmanager
def _spy_capture(stdout: str = "", stderr: str = "", capped: bool = False, raise_exc=None):
    """Patch the bounded-capture seam ``_capture_bounded``.

    After the round-20 OOM-cap refactor the command executor no longer calls
    ``proc.communicate(input=, timeout=)``; it drives the process through
    ``_capture_bounded(proc, stdin_text, timeout)`` (threaded stdin-feed +
    per-stream drain with a hard cap). This records the ``stdin_text`` payload
    and the ``timeout`` budget threaded into capture, and returns the
    configured ``(stdout, stderr, capped)`` tuple -- or raises ``raise_exc``
    (e.g. ``subprocess.TimeoutExpired``) to model a timeout.
    """
    calls: dict = {}

    def _fake(proc, stdin_text, timeout, cap=None):
        calls["proc"] = proc
        calls["stdin_text"] = stdin_text
        calls["timeout"] = timeout
        if raise_exc is not None:
            raise raise_exc
        return (stdout, stderr, capped)

    with patch("apm_cli.core.script_executors._capture_bounded", side_effect=_fake):
        yield calls


# -- execute_script dispatcher ---------------------------------------------


# -- HTTP executor ---------------------------------------------------------


# -- Command executor ------------------------------------------------------


class TestCommandExecutor:
    def test_runs_command_with_stdin_payload(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="echo done")
        event = _make_event()
        patcher, _ = _patch_popen()
        with patcher, _spy_capture() as cap:
            _execute_command(script, event)
        input_data = cap["stdin_text"]
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
        patcher, _ = _patch_popen()
        with patcher, _spy_capture() as cap:
            _execute_command(script, _make_event())
        assert cap["timeout"] == 5

    def test_default_timeout(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="echo")
        patcher, _ = _patch_popen()
        with patcher, _spy_capture() as cap:
            _execute_command(script, _make_event())
        assert cap["timeout"] == 30

    def test_swallows_timeout_error(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="sleep")
        patcher, _ = _patch_popen()
        with patcher, _spy_capture(raise_exc=subprocess.TimeoutExpired("sleep", 30)):
            _execute_command(script, _make_event())

    def test_verbose_logs_on_timeout(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="slow")
        logger = MagicMock()
        patcher, _ = _patch_popen()
        with patcher, _spy_capture(raise_exc=subprocess.TimeoutExpired("slow", 30)):
            _execute_command(script, _make_event(), logger=logger, verbose=True)
        # Timeout warning always emits (not verbose-gated); uses logger.warning,
        # which adds the [!] symbol itself, so the message must not repeat it.
        logger.warning.assert_called_once()
        message = logger.warning.call_args[0][0]
        assert not message.startswith("[!]")
        assert "post-install command script timed out after 30s" in message
        assert "slow" not in message

    def test_warns_on_nonzero_exit(self) -> None:
        script = ScriptEntry(script_type="command", event="pre-compile", bash="false")
        logger = MagicMock()
        patcher, _ = _patch_popen(returncode=2)
        with patcher, _spy_capture():
            _execute_command(script, _make_event("pre-compile"), logger=logger)
        logger.warning.assert_called_once()
        message = logger.warning.call_args[0][0]
        assert "pre-compile command script exited with code 2; continuing" in message
        assert "scripts.log" in message

    def test_capped_output_warns_once_not_also_for_exit_code(self) -> None:
        script = ScriptEntry(script_type="command", event="pre-compile", bash="noisy")
        logger = MagicMock()
        patcher, _ = _patch_popen(returncode=-9)
        with patcher, _spy_capture(capped=True):
            _execute_command(script, _make_event("pre-compile"), logger=logger)
        logger.warning.assert_called_once()
        assert "exceeded the capture limit" in logger.warning.call_args[0][0]

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

    @pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups")
    def test_interrupt_reaps_script_group_before_propagating(self, tmp_path: Path) -> None:
        """Ctrl+C never reaches the script's own session, so APM must reap it."""
        pid_file = tmp_path / "grandchild.pid"
        script = ScriptEntry(
            script_type="command",
            event="pre-compile",
            bash=f"sleep 60 & echo $! > '{pid_file}'; wait",
            timeout_sec=60,
        )

        def _interrupt_once_running(proc, stdin_text, timeout, cap=None):
            deadline = time.monotonic() + 10
            while not (pid_file.exists() and pid_file.read_text(encoding="ascii").strip()):
                assert time.monotonic() < deadline, "script never started"
                time.sleep(0.02)
            raise KeyboardInterrupt

        with (
            patch(
                "apm_cli.core.script_executors._capture_bounded",
                side_effect=_interrupt_once_running,
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            _execute_command(script, _make_event("pre-compile"), project_root=str(tmp_path))

        grandchild = int(pid_file.read_text(encoding="ascii"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("interrupted lifecycle script outlived the APM command")


# -- _expand_env_vars ------------------------------------------------------


# -- _build_script_env -----------------------------------------------------


# -- _resolve_cwd ----------------------------------------------------------


class TestResolveCwd:
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
    @pytest.mark.windows_compat
    def test_respects_apm_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # "/custom/apm" is drive-relative on Windows, so get_apm_home() anchors
        # it to the startup drive; use a path that is absolute on every OS.
        apm_home = tmp_path / "custom" / "apm"
        monkeypatch.setenv("APM_HOME", str(apm_home))
        path = _get_scripts_log_path()
        assert path == apm_home / "logs" / "scripts.log"


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


class TestCommandExecutorLogging:
    def test_logs_successful_command_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        script = ScriptEntry(script_type="command", event="post-install", bash="echo done")
        patcher, _ = _patch_popen(returncode=0)
        with patcher, _spy_capture(stdout="script output line", stderr=""):
            _execute_command(script, _make_event())
        content = (tmp_path / "logs" / "scripts.log").read_text()
        assert "script output line" in content
        assert "exit_code=0" in content
        assert "status=ok" in content

    def test_logs_failed_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        script = ScriptEntry(script_type="command", event="post-install", bash="false")
        patcher, _ = _patch_popen(returncode=1)
        with patcher, _spy_capture(stdout="", stderr="something broke"):
            _execute_command(script, _make_event())
        content = (tmp_path / "logs" / "scripts.log").read_text()
        assert "something broke" in content
        assert "exit_code=1" in content
        assert "status=error" in content

    def test_logs_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        script = ScriptEntry(script_type="command", event="post-install", bash="sleep 999")
        patcher, _ = _patch_popen()
        with patcher, _spy_capture(raise_exc=subprocess.TimeoutExpired("sleep", 30)):
            _execute_command(script, _make_event())
        content = (tmp_path / "logs" / "scripts.log").read_text()
        assert "status=timeout" in content


# -- URL redaction ---------------------------------------------------------
