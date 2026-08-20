"""RED-TEAM: malformed timeout / cwd / NUL reaching the command subprocess.

A bad ``timeoutSec`` (string, negative, bool) is stored verbatim and
handed to ``subprocess.run(timeout=...)`` by ``_execute_command``. The
contract under test: firing such a script through
``LifecycleScriptRunner.fire`` must (a) never let an exception escape and
(b) never hang -- a negative or zero timeout must fail fast, not wedge the
install. All execution-time crashes (``TypeError`` from a string timeout,
``TypeError`` from a non-string ``cwd``, ``ValueError`` from an embedded
NUL byte) must be swallowed by the executor / runner isolation.

Every fire is wall-clock guarded so a regression that DID hang is caught
in a daemon thread instead of stalling CI.
"""

from __future__ import annotations

import pytest

from .conftest import run_guarded


@pytest.fixture()
def fire_env(tmp_path, monkeypatch):
    """Hermetic APM_HOME so script logs land in tmp, plus an event factory."""
    home = tmp_path / "apm_home"
    home.mkdir()
    monkeypatch.setenv("APM_HOME", str(home))
    from apm_cli.core.lifecycle_scripts import LifecycleEvent

    event = LifecycleEvent(event="post-install", working_directory=str(tmp_path))
    return event


def _fire(entry, event, project_root):
    from apm_cli.core.lifecycle_scripts import LifecycleScriptRunner

    runner = LifecycleScriptRunner(scripts=[entry], project_root=str(project_root))
    return runner.fire("post-install", event)


def _command_entry(**overrides):
    from apm_cli.core.lifecycle_scripts import ScriptEntry

    base = dict(script_type="command", event="post-install", bash="echo hi", command="echo hi")
    base.update(overrides)
    return ScriptEntry(**base)


@pytest.mark.parametrize("bad_timeout", ["abc", -5, True, 1.5])
def test_bad_timeout_does_not_escape_or_hang(tmp_path, fire_env, bad_timeout):
    entry = _command_entry(timeout_sec=bad_timeout)
    finished, _result, exc = run_guarded(lambda: _fire(entry, fire_env, tmp_path), timeout=8.0)
    assert finished, f"fire() hung with timeout_sec={bad_timeout!r}"
    assert exc is None, f"fire() leaked an exception with timeout_sec={bad_timeout!r}: {exc!r}"


def test_negative_timeout_fails_fast_no_hang(tmp_path, fire_env):
    """A negative timeout must not block -- subprocess raises TimeoutExpired
    immediately, which the executor catches."""
    entry = _command_entry(bash="sleep 30", command="sleep 30", timeout_sec=-1)
    finished, _result, exc = run_guarded(lambda: _fire(entry, fire_env, tmp_path), timeout=5.0)
    assert finished, "negative timeout caused a hang (sleep was not pre-empted)"
    assert exc is None, f"negative timeout leaked: {exc!r}"


def test_string_timeout_typeerror_is_swallowed(tmp_path, fire_env):
    """timeoutSec:'abc' -> subprocess TypeError -> caught, no escape."""
    entry = _command_entry(timeout_sec="abc")
    threads = None
    try:
        threads = _fire(entry, fire_env, tmp_path)
    except Exception as exc:
        pytest.fail(f"string timeout escaped fire(): {type(exc).__name__}: {exc}")
    assert threads == []


def test_non_string_cwd_typeerror_is_isolated(tmp_path, fire_env):
    """cwd:int -> Path(int) TypeError in _resolve_cwd -> caught by fire()."""
    entry = _command_entry(cwd=12345)
    finished, _result, exc = run_guarded(lambda: _fire(entry, fire_env, tmp_path), timeout=6.0)
    assert finished, "fire() hung on int cwd"
    assert exc is None, f"int cwd leaked an exception from fire(): {exc!r}"


def test_nul_byte_in_command_is_isolated(tmp_path, fire_env):
    """An embedded NUL -> subprocess ValueError -> caught, never escapes."""
    entry = _command_entry(bash="echo \x00boom", command="echo \x00boom")
    finished, _result, exc = run_guarded(lambda: _fire(entry, fire_env, tmp_path), timeout=6.0)
    assert finished, "fire() hung on NUL command"
    assert exc is None, f"NUL command leaked an exception from fire(): {exc!r}"


def test_huge_timeout_with_fast_command_does_not_hang(tmp_path, fire_env):
    """A huge timeout is harmless when the command exits promptly."""
    entry = _command_entry(bash="echo done", command="echo done", timeout_sec=999999999)
    finished, _result, exc = run_guarded(lambda: _fire(entry, fire_env, tmp_path), timeout=6.0)
    assert finished, "huge timeout with a fast command should not block"
    assert exc is None, f"huge timeout leaked: {exc!r}"
