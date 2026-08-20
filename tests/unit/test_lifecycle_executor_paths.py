"""Unit coverage for the lifecycle script *execution* paths.

The command/HTTP executor, env-builder, SSRF gate, and log-writer in
``apm_cli.core.script_executors`` carry the bulk of the module's logic but were
previously exercised only by the adversarial red-team suite (which CI does not
collect). This module pins their observable behaviour inside the collected unit
suite -- a real regression in any of these paths now fails a normal CI shard.

The assertions here deliberately target end-to-end side effects (the rendered
``scripts.log`` line, the filtered child environment, the returned thread) so
they remain meaningful even as the internal helpers are refactored.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import LifecycleEvent, ScriptEntry


@pytest.fixture
def apm_log_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the scripts log at an isolated APM_HOME and return its log path."""
    home = tmp_path / "apmhome"
    monkeypatch.setenv("APM_HOME", str(home))
    return home / "logs" / "scripts.log"


def _event(name: str = "post-install") -> LifecycleEvent:
    return LifecycleEvent.create(event=name, working_directory="/tmp")


def _cmd(command: str, **kw) -> ScriptEntry:
    return ScriptEntry(script_type="command", event="post-install", command=command, **kw)


# ---------------------------------------------------------------------------
# Command executor (execute_script -> _execute_command -> log)
# ---------------------------------------------------------------------------


class TestCommandExecutor:
    def test_successful_command_logs_ok_and_returns_none(self, apm_log_home: Path) -> None:
        result = se.execute_script(_cmd("echo hello-from-script"), _event())
        assert result is None
        contents = apm_log_home.read_text()
        assert "event=post-install" in contents
        assert "status=ok" in contents
        assert "exit_code=0" in contents

    def test_nonzero_exit_logs_error_status(self, apm_log_home: Path) -> None:
        se.execute_script(_cmd("exit 7"), _event())
        line = apm_log_home.read_text()
        assert "status=error" in line
        assert "exit_code=7" in line

    def test_empty_command_is_a_noop(self, apm_log_home: Path) -> None:
        se.execute_script(_cmd(""), _event())
        assert not apm_log_home.exists()

    def test_stdout_secret_is_redacted_in_log(self, apm_log_home: Path) -> None:
        secret = "tokenvalue" + "ABCDEFGH1234"
        monkey = {"MY_SECRET_TOKEN": secret}
        with patch.dict(os.environ, monkey):
            se.execute_script(_cmd(f"echo {secret}"), _event())
        written = apm_log_home.read_text()
        assert secret not in written
        assert "stdout:" in written

    def test_timeout_path_logs_timeout(self, apm_log_home: Path) -> None:
        import sys

        # Use a portable long-running process (python sleep) so the 1s timeout
        # reliably fires on every platform -- ``sleep`` is not a Windows shell
        # builtin, so a literal "sleep 5" exits immediately on Windows.
        entry = _cmd(f'{sys.executable} -c "import time; time.sleep(5)"', timeout_sec=1)
        se.execute_script(entry, _event())
        assert "status=timeout" in apm_log_home.read_text()


# ---------------------------------------------------------------------------
# Environment construction
# ---------------------------------------------------------------------------


class TestBuildScriptEnv:
    def test_credential_named_var_is_stripped(self) -> None:
        with patch.dict(os.environ, {"DEPLOY_SECRET": "x", "PLAIN_FLAG": "1"}):
            env = se._build_script_env(_cmd("true"))
        assert "DEPLOY_SECRET" not in env
        assert env.get("PLAIN_FLAG") == "1"

    def test_allowed_env_var_is_kept(self) -> None:
        entry = _cmd("true", allowed_env_vars=["ANALYTICS_TOKEN"])
        with patch.dict(os.environ, {"ANALYTICS_TOKEN": "keepme"}):
            env = se._build_script_env(entry)
        assert env.get("ANALYTICS_TOKEN") == "keepme"

    def test_script_env_override_merges_last(self) -> None:
        entry = _cmd("true", env={"EXTRA_VAR": "configured"})
        env = se._build_script_env(entry)
        assert env.get("EXTRA_VAR") == "configured"


# ---------------------------------------------------------------------------
# $VAR expansion gate
# ---------------------------------------------------------------------------


class TestExpandEnvVars:
    def test_plain_var_expands(self) -> None:
        with patch.dict(os.environ, {"REGION_NAME": "euw"}):
            assert se._expand_env_vars("region=$REGION_NAME") == "region=euw"

    def test_braced_var_expands(self) -> None:
        with patch.dict(os.environ, {"BUILD_ID": "42"}):
            assert se._expand_env_vars("id=${BUILD_ID}") == "id=42"

    def test_denylisted_var_blocked_to_empty(self) -> None:
        with patch.dict(os.environ, {"API_TOKEN": "nope"}):
            assert se._expand_env_vars("h=$API_TOKEN") == "h="

    def test_allowed_denylisted_var_expands(self) -> None:
        with patch.dict(os.environ, {"API_TOKEN": "ok"}):
            out = se._expand_env_vars("h=$API_TOKEN", frozenset({"API_TOKEN"}))
        assert out == "h=ok"

    def test_crlf_smuggle_stripped_from_expansion(self) -> None:
        with patch.dict(os.environ, {"UA_STRING": "agent\r\nX-Evil: 1"}):
            out = se._expand_env_vars("$UA_STRING")
        assert "\r" not in out and "\n" not in out


# ---------------------------------------------------------------------------
# SSRF gate helpers
# ---------------------------------------------------------------------------


class TestSsrfGate:
    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "localhost", "169.254.169.254", "10.0.0.5", "192.168.1.1", "::1"],
    )
    def test_internal_hosts_are_blocked(self, host: str) -> None:
        assert se._ssrf_block_reason(host) is not None

    def test_public_host_literal_is_allowed(self) -> None:
        assert se._ssrf_block_reason("93.184.216.34") is None

    def test_ip_is_internal_classifies_loopback(self) -> None:
        import ipaddress

        assert se._ip_is_internal(ipaddress.ip_address("127.0.0.1")) is True
        assert se._ip_is_internal(ipaddress.ip_address("8.8.8.8")) is False


# ---------------------------------------------------------------------------
# HTTP executor (mocked transport -- no real network)
# ---------------------------------------------------------------------------


def _https(url: str, **kw) -> ScriptEntry:
    return ScriptEntry(script_type="http", event="post-install", url=url, **kw)


class TestHttpExecutor:
    def test_internal_url_refused_returns_none(self, apm_log_home: Path) -> None:
        thread = se.execute_script(_https("https://127.0.0.1/hook"), _event())
        assert thread is None

    def test_non_https_url_refused(self, apm_log_home: Path) -> None:
        assert se.execute_script(_https("http://example.com/hook"), _event()) is None

    def test_public_url_dispatches_and_logs(self, apm_log_home: Path) -> None:
        fake_resp = MagicMock(status_code=200, ok=True)
        fake_session = MagicMock()
        fake_session.post.return_value = fake_resp
        with patch.object(se, "_get_guarded_session", return_value=fake_session):
            thread = se.execute_script(_https("https://example.com/hook"), _event())
            assert isinstance(thread, threading.Thread)
            thread.join(timeout=5)
        assert fake_session.post.called
        assert "type=http" in apm_log_home.read_text()

    def test_batch_dispatch_drains_all_entries(self, apm_log_home: Path) -> None:
        fake_session = MagicMock()
        fake_session.post.return_value = MagicMock(status_code=204, ok=True)
        entries = [_https(f"https://example.com/h{i}") for i in range(4)]
        with patch.object(se, "_get_guarded_session", return_value=fake_session):
            workers = se.dispatch_http_batch(entries, _event())
            for worker in workers:
                worker.join(timeout=5)
        assert fake_session.post.call_count == 4

    def test_empty_batch_returns_no_workers(self) -> None:
        assert se.dispatch_http_batch([], _event()) == []


# ---------------------------------------------------------------------------
# Log path + rotation
# ---------------------------------------------------------------------------


class TestLogRotation:
    def test_log_path_honours_apm_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        assert se._get_scripts_log_path() == tmp_path / "logs" / "scripts.log"

    def test_oversized_log_is_rotated(self, apm_log_home: Path) -> None:
        apm_log_home.parent.mkdir(parents=True, exist_ok=True)
        apm_log_home.write_bytes(b"x" * (se._MAX_LOG_BYTES + 1))
        se._rotate_log_if_large(apm_log_home)
        assert apm_log_home.with_name("scripts.log.1").exists()


# ---------------------------------------------------------------------------
# Guarded session / connect-layer transport internals
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_guarded_session():
    """Clear the process-cached guarded session before and after the test."""
    se._GUARDED_SESSION = None
    yield
    se._GUARDED_SESSION = None


class TestGuardedSession:
    def test_build_guarded_session_returns_session(self, reset_guarded_session) -> None:
        import requests

        session = se._build_guarded_session()
        assert isinstance(session, requests.Session)

    def test_get_guarded_session_is_cached(self, reset_guarded_session) -> None:
        first = se._get_guarded_session()
        second = se._get_guarded_session()
        assert first is second

    def test_get_guarded_session_none_on_build_failure(self, reset_guarded_session) -> None:
        with patch.object(se, "_build_guarded_session", side_effect=RuntimeError("boom")):
            assert se._get_guarded_session() is None


class TestConnectLayerHost:
    def test_plain_url_host(self) -> None:
        assert se._connect_layer_host("https://example.com/path") == "example.com"

    def test_backslash_authority_differs_from_urlparse(self) -> None:
        # urllib3 terminates the authority at the backslash; urllib.parse does not.
        connect = se._connect_layer_host("https://169.254.169.254\\.evil.com/")
        assert connect == "169.254.169.254"

    def test_normalize_host_strips_brackets_and_cases(self) -> None:
        assert se._normalize_host("[::1]") == "::1"
        assert se._normalize_host("EXAMPLE.com") == "example.com"
        assert se._normalize_host(None) == ""


class TestSafeUrlparse:
    def test_valid_url_parsed(self) -> None:
        assert se._safe_urlparse("https://example.com/").scheme == "https"

    def test_malformed_ipv6_returns_none(self) -> None:
        assert se._safe_urlparse("https://[::1\\.evil/") is None


class TestSsrfSafeConnect:
    def test_blocked_internal_resolution_raises(self) -> None:
        infos = [(2, 1, 6, "", ("169.254.169.254", 443))]
        with patch("socket.getaddrinfo", return_value=infos):
            with pytest.raises(se._SSRFConnectError):
                se._ssrf_safe_connect(("rebind.example", 443))

    def test_public_resolution_connects(self) -> None:
        infos = [(2, 1, 6, "", ("93.184.216.34", 443))]
        fake_sock = MagicMock()
        with patch("socket.getaddrinfo", return_value=infos):
            with patch("socket.socket", return_value=fake_sock):
                result = se._ssrf_safe_connect(("public.example", 443), timeout=3)
        assert result is fake_sock
        assert fake_sock.connect.called


class TestPrepareHttpRejections:
    def test_missing_url_returns_none(self) -> None:
        entry = ScriptEntry(script_type="http", event="post-install", url=None)
        assert se._prepare_http(entry, _event()) is None

    def test_malformed_url_returns_none(self) -> None:
        entry = _https("https://[::1\\.evil/")
        assert se._prepare_http(entry, _event()) is None

    def test_denylisted_header_var_blocked_in_prepared_headers(self) -> None:
        entry = _https(
            "https://example.com/hook",
            headers={"X-Auth": "$SECRET_TOKEN", "X-Plain": "static"},
        )
        with patch.dict(os.environ, {"SECRET_TOKEN": "leakme"}):
            prepared = se._prepare_http(entry, _event())
        assert prepared is not None
        headers = prepared[2]
        assert headers["X-Auth"] == ""
        assert headers["X-Plain"] == "static"


class TestHttpPayloadMinimisation:
    def test_working_directory_reduced_to_basename(self) -> None:
        import json

        event = LifecycleEvent.create(event="post-install", working_directory="/home/alice/proj")
        payload = json.loads(se._http_payload(event))
        assert payload["working_directory"] == "proj"


class TestKillProcessGroup:
    def test_none_proc_is_noop(self) -> None:
        se._kill_process_group(None)  # must not raise

    def test_running_proc_is_reaped(self) -> None:
        import subprocess

        proc = subprocess.Popen("sleep 30", shell=True, start_new_session=True)
        se._kill_process_group(proc)
        assert proc.poll() is not None


class TestResolveCwd:
    """Coverage for _resolve_cwd containment (relative escape -> project root)."""

    def test_no_cwd_returns_project_root(self) -> None:
        assert se._resolve_cwd(_cmd("echo x"), "/proj") == "/proj"

    def test_absolute_cwd_passthrough(self, tmp_path: Path) -> None:
        target = str(tmp_path)
        entry = _cmd("echo x", cwd=target)
        assert se._resolve_cwd(entry, str(tmp_path)) == target

    def test_relative_within_root_resolves(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        entry = _cmd("echo x", cwd="sub")
        resolved = se._resolve_cwd(entry, str(tmp_path))
        assert resolved == str((tmp_path / "sub").resolve())

    def test_relative_escape_clamped_to_root(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        entry = _cmd("echo x", cwd="../../etc")
        resolved = se._resolve_cwd(entry, str(root))
        assert resolved == str(root.resolve())


class TestCommandExecutorFailureBranches:
    """Timeout and generic-exception branches of _execute_command."""

    def test_timeout_logs_timeout_and_reaps(self, apm_log_home: Path) -> None:
        import subprocess

        fake = MagicMock()
        fake.pid = 999999
        logger = MagicMock()
        with (
            patch.object(se.subprocess, "Popen", return_value=fake),
            patch.object(
                se,
                "_capture_bounded",
                side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1),
            ),
            patch.object(se, "_kill_process_group") as killer,
        ):
            se.execute_script(_cmd("sleep 100"), _event(), logger=logger)
        killer.assert_called_once_with(fake)
        assert "status=timeout" in apm_log_home.read_text()

    def test_generic_exception_logs_error_and_reaps(self, apm_log_home: Path) -> None:
        fake = MagicMock()
        fake.pid = 999998
        logger = MagicMock()
        with (
            patch.object(se.subprocess, "Popen", return_value=fake),
            patch.object(se, "_capture_bounded", side_effect=RuntimeError("boom")),
            patch.object(se, "_kill_process_group") as killer,
        ):
            se.execute_script(_cmd("do-thing"), _event(), logger=logger, verbose=True)
        killer.assert_called_once_with(fake)
        assert "status=error" in apm_log_home.read_text()

    def test_slow_command_emits_warning(self, apm_log_home: Path) -> None:
        fake = MagicMock()
        fake.returncode = 0
        fake.pid = 999997
        logger = MagicMock()
        times = iter([0.0, 999.0])
        with (
            patch.object(se.subprocess, "Popen", return_value=fake),
            patch.object(se, "_capture_bounded", return_value=("out", "", False)),
            patch.object(se.time, "monotonic", side_effect=lambda: next(times)),
        ):
            se.execute_script(_cmd("slowcmd"), _event(), logger=logger)
        assert logger.warning.called


class TestSsrfBlockReasonResolverGuard:
    """_ssrf_block_reason fails closed (None) when name resolution raises."""

    def test_oserror_resolution_returns_none(self) -> None:
        with patch.object(se.socket, "getaddrinfo", side_effect=OSError("no name")):
            assert se._ssrf_block_reason("nonexistent.invalid") is None

    def test_valueerror_resolution_returns_none(self) -> None:
        with patch.object(se.socket, "getaddrinfo", side_effect=ValueError("nul")):
            assert se._ssrf_block_reason("bad\x00host") is None


def _http(url: str, **kw) -> ScriptEntry:
    return ScriptEntry(script_type="http", event="post-install", url=url, **kw)


class TestDispatchHttpRequest:
    """_dispatch_http_request logs status on success and errors on failure."""

    def test_success_logs_status(self, apm_log_home: Path) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        with patch.object(se, "_get_guarded_session", return_value=None):
            with patch.object(se, "_get_capturing_session", return_value=None):
                with patch("requests.post", return_value=resp) as post:
                    se._dispatch_http_request(
                        "https://example.com/h",
                        "{}",
                        {"Content-Type": "application/json"},
                        5.0,
                        "post-install",
                        "https://example.com/h",
                    )
        post.assert_called_once()
        log = apm_log_home.read_text()
        assert "HTTP 200" in log
        assert "status=ok" in log

    def test_non_ok_status_logs_error(self, apm_log_home: Path) -> None:
        resp = MagicMock()
        resp.status_code = 503
        resp.ok = False
        with patch.object(se, "_get_guarded_session", return_value=None):
            with patch.object(se, "_get_capturing_session", return_value=None):
                with patch("requests.post", return_value=resp):
                    se._dispatch_http_request(
                        "https://example.com/h",
                        "{}",
                        {},
                        5.0,
                        "post-install",
                        "https://example.com/h",
                    )
        assert "status=error" in apm_log_home.read_text()

    def test_exception_logs_error(self, apm_log_home: Path) -> None:
        with patch.object(se, "_get_guarded_session", return_value=None):
            with patch.object(se, "_get_capturing_session", return_value=None):
                with patch("requests.post", side_effect=RuntimeError("conn refused")):
                    se._dispatch_http_request(
                        "https://example.com/h",
                        "{}",
                        {},
                        5.0,
                        "post-install",
                        "https://example.com/h",
                    )
        assert "status=error" in apm_log_home.read_text()


class TestExecuteHttpThread:
    """_execute_http starts a daemon thread and joins cleanly; gate refusal -> None."""

    def test_refused_destination_returns_none(self) -> None:
        thread = se._execute_http(_http("http://example.com/insecure"), _event())
        assert thread is None

    def test_https_destination_starts_thread(self) -> None:
        logger = MagicMock()
        with patch.object(se, "_dispatch_http_request") as dispatch:
            thread = se._execute_http(
                _http("https://example.com/hook"), _event(), logger=logger, verbose=True
            )
        assert isinstance(thread, threading.Thread)
        thread.join(timeout=5)
        dispatch.assert_called_once()
        assert logger.verbose_detail.called
