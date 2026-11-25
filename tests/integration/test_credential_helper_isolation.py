"""The test suite never reaches the host's git credential helpers (#5474).

On macOS runners the system git config names ``osxkeychain``. Unit tests
whose auth chain fell through to ``git credential fill`` waited on a blocked
keychain until the 60s timeout killed ``git``, orphaning ~120 helpers and
adding ~25 minutes to the macOS unit job. The root conftest now resets the
helper list at system scope, and a session guard fails the run when a
``git-credential-*`` process outlives it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.utils import credential_helpers

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-host-credential-resolution"),
]

ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")


def _git(*args: str, env: dict[str, str], cwd: Path, stdin: str | None = None):
    return subprocess.run(
        [GIT, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=60,
        check=False,
    )


@needs_git
def test_session_git_resolves_an_empty_credential_helper(tmp_path: Path) -> None:
    """Even an env that drops GIT_CONFIG_NOSYSTEM to reach user helpers finds none.

    Production does exactly that for helper-backed fallbacks
    (``GitAuthEnvBuilder.noninteractive_env``).
    """
    env = dict(os.environ)
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    result = _git("config", "--get", "credential.helper", env=env, cwd=tmp_path)

    assert result.returncode == 0, "no credential.helper reset is configured for the session"
    assert result.stdout.strip() == ""


@needs_git
def test_isolated_system_config_skips_a_system_helper_and_keeps_other_keys(
    tmp_path: Path,
) -> None:
    """A helper in the real system config never runs; its other keys still apply."""
    hits = tmp_path / "helper-hits.log"
    real_system = tmp_path / "real-system.gitconfig"
    real_system.write_text(
        "[apm]\n\tprobe = kept\n"
        f"[credential]\n\thelper = \"!f() {{ echo hit >> '{hits.as_posix()}'; }}; f\"\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "GIT_CONFIG_SYSTEM": str(real_system),
        "HOME": str(home),
        "GIT_TERMINAL_PROMPT": "0",
        "XDG_CONFIG_HOME": str(home / "xdg"),
    }
    env.pop("GIT_CONFIG_GLOBAL", None)
    env.pop("GIT_CONFIG_NOSYSTEM", None)

    credential_helpers.isolate_git_credential_helpers(tmp_path, env)
    _git(
        "credential",
        "fill",
        env=env,
        cwd=tmp_path,
        stdin="protocol=https\nhost=example.invalid\n\n",
    )

    assert "XDG_CONFIG_HOME" not in env
    assert not hits.exists(), "the system credential helper ran"
    probe = _git("config", "--get", "apm.probe", env=env, cwd=tmp_path)
    assert probe.stdout.strip() == "kept"


_LEAKING_TEST = """
import pathlib
import subprocess

def test_leaves_a_credential_helper_running():
    helper = subprocess.Popen(
        [{helper!r}, "120"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session={detach!r},
    )
    pathlib.Path({pid_file!r}).write_text(str(helper.pid))
"""

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


def _fake_helper(tmp_path: Path) -> Path:
    sleep = shutil.which("sleep")
    assert sleep is not None
    helper = tmp_path / "git-credential-apmleak"
    shutil.copy2(sleep, helper)
    return helper


def _run_leaking_session(
    tmp_path: Path, *, detach: bool
) -> tuple[subprocess.CompletedProcess, int]:
    """Run an inner pytest session whose only test leaves a fake helper running."""
    pid_file = tmp_path / "helper.pid"
    test_file = tmp_path / "test_leak.py"
    test_file.write_text(
        textwrap.dedent(
            _LEAKING_TEST.format(
                helper=str(_fake_helper(tmp_path)), detach=detach, pid_file=str(pid_file)
            )
        ),
        "utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.utils.credential_helpers",
            "-q",
            str(test_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result, int(pid_file.read_text())


def _alive(pid: int) -> bool:
    return pid in credential_helpers.credential_helper_processes()


def _kill(pid: int) -> None:
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


@posix_only
def test_guard_fails_the_session_that_leaves_a_credential_helper(tmp_path: Path) -> None:
    """A helper this pytest run started that outlives it fails the session and is stopped."""
    result, pid = _run_leaking_session(tmp_path, detach=False)
    try:
        survived = _alive(pid)
    finally:
        _kill(pid)

    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, output[-3000:]
    assert "1 passed" in output
    assert "git credential helper process(es) started during the run" in output
    assert f"pid {pid}:" in output
    assert not survived, "the guard did not terminate the leaked helper"


@posix_only
def test_guard_ignores_a_helper_outside_its_process_group(tmp_path: Path) -> None:
    """Another process group's helper (another run, a user's git) is neither reported nor stopped."""
    result, pid = _run_leaking_session(tmp_path, detach=True)
    try:
        survived = _alive(pid)
    finally:
        _kill(pid)

    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.OK, output[-3000:]
    assert "git credential helper" not in output
    assert survived, "the guard stopped a helper it does not own"


@posix_only
def test_exited_unreaped_helper_is_not_listed_as_running(tmp_path: Path) -> None:
    """A zombie helper is not a leak: it has exited and only awaits its reaper."""
    helper = subprocess.Popen([str(_fake_helper(tmp_path)), "120"], stdin=subprocess.DEVNULL)
    try:
        assert helper.pid in credential_helpers.credential_helper_processes(os.getpgid(0))
        os.kill(helper.pid, signal.SIGKILL)
        os.waitid(os.P_PID, helper.pid, os.WEXITED | os.WNOWAIT)

        assert helper.pid not in credential_helpers.credential_helper_processes(os.getpgid(0))
    finally:
        _kill(helper.pid)
        helper.wait(timeout=30)


def test_root_conftest_registers_the_guard(pytestconfig: pytest.Config) -> None:
    """The suite carries the guard (on xdist it lives on the controller)."""
    if hasattr(pytestconfig, "workerinput"):
        pytest.skip("the guard runs on the xdist controller")
    assert pytestconfig.pluginmanager.has_plugin(credential_helpers.PLUGIN_NAME)
