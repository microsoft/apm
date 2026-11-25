"""Keep the test suite away from the host's git credential helpers (#5474).

The root ``tests/conftest.py`` points ``GIT_CONFIG_SYSTEM`` at a file that
includes the real system config and then resets ``credential.helper`` to the
empty list, and drops ``GIT_CONFIG_GLOBAL`` and ``XDG_CONFIG_HOME`` (``HOME``
is already a temp dir).
Every git a test spawns -- including the ``git credential fill`` that the auth
chain runs when no token is set -- then finds no ambient helper. On macOS
runners the system config names ``osxkeychain``; a blocked keychain held each
fill until the 60s timeout killed ``git`` and orphaned the helper, adding
about 25 minutes to the macOS unit job and leaving ~120 helper processes.

Production code keeps ``GIT_CONFIG_SYSTEM`` (``apm_cli.utils.git_env``), so the
reset survives envs that drop ``GIT_CONFIG_NOSYSTEM`` to reach user helpers.
Tests that need a helper configure one at global or command scope, which
still applies after the system-level reset.

:class:`CredentialHelperLeakGuard` fails the session when a
``git-credential-*`` process this pytest invocation started is still alive at
the end, and terminates it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path

import pytest

HELPER_PREFIX = "git-credential"
PLUGIN_NAME = "apm-credential-helper-leak-guard"
# A helper still running this long after the last test is not a live
# exchange: git waits for its helper, so only an orphan outlives its git.
_GRACE_SECONDS = 2.0


def system_config_text(real_system_config: str | None) -> str:
    """Git config text: the real system config, then an empty helper list."""
    lines: list[str] = []
    if real_system_config:
        include = Path(real_system_config).as_posix().replace('"', '\\"')
        lines += ["[include]", f'\tpath = "{include}"']
    lines += ["[credential]", "\thelper ="]
    return "\n".join(lines) + "\n"


def _real_system_config(environ: MutableMapping[str, str]) -> str | None:
    if environ.get("GIT_CONFIG_SYSTEM"):
        return environ["GIT_CONFIG_SYSTEM"]
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "var", "GIT_CONFIG_SYSTEM"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = result.stdout.strip()
    return path if result.returncode == 0 and path else None


def isolate_git_credential_helpers(
    root: Path, environ: MutableMapping[str, str] = os.environ
) -> Path:
    """Write the isolated system config under *root* and point git at it."""
    config = root / "gitconfig-system"
    config.write_text(system_config_text(_real_system_config(environ)), encoding="utf-8")
    environ["GIT_CONFIG_SYSTEM"] = str(config)
    # HOME is a temp dir; an inherited global or XDG config would bypass it.
    environ.pop("GIT_CONFIG_GLOBAL", None)
    environ.pop("XDG_CONFIG_HOME", None)
    return config


def credential_helper_processes(pgid: int | None = None) -> dict[int, str]:
    """Live ``git-credential-*`` processes in process group *pgid*, pid -> name.

    Helpers inherit the process group of the pytest invocation that spawned
    them through xdist workers and git, and keep it after their parent dies,
    so the group marks ownership across reparenting. Zombies are skipped: an
    exited helper waiting to be reaped is not a leak. POSIX only; Windows
    neither reparents nor exposes process groups, so nothing is listed there.
    """
    if sys.platform == "win32":
        return {}
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,stat=,comm="],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    helpers: dict[int, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 4 or not (fields[0].isdigit() and fields[1].isdigit()):
            continue
        pid, group, state, command = int(fields[0]), int(fields[1]), fields[2], fields[3]
        if state.startswith("Z") or (pgid is not None and group != pgid):
            continue
        # Linux truncates ``comm`` to 15 characters: "git-credential-".
        if Path(command.strip()).name.startswith(HELPER_PREFIX):
            helpers[pid] = command.strip()
    return helpers


def _terminate(pids: list[int]) -> None:
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


class CredentialHelperLeakGuard:
    """Fail the run when a test leaves a ``git-credential-*`` process behind.

    Runs on the xdist controller (or the only process): helpers outlive the
    worker whose ``git`` was killed, so a per-worker check would miss them.
    Only helpers in this invocation's process group that were not running at
    session start count, so another pytest run or git command on the machine
    is never reported or stopped.
    """

    def __init__(
        self,
        list_helpers: Callable[[int | None], dict[int, str]] = credential_helper_processes,
        grace_seconds: float = _GRACE_SECONDS,
    ) -> None:
        self._list_helpers = list_helpers
        self._grace_seconds = grace_seconds
        self._pgid: int | None = None
        self._before: set[int] = set()

    def _new_helpers(self) -> dict[int, str]:
        return {
            pid: command
            for pid, command in self._list_helpers(self._pgid).items()
            if pid not in self._before
        }

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self._pgid = os.getpgid(0) if hasattr(os, "getpgid") else None
        self._before = set(self._list_helpers(self._pgid))

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self._pgid is None:
            return
        leaked = self._new_helpers()
        if leaked and self._grace_seconds:
            time.sleep(self._grace_seconds)
            still = self._new_helpers()
            leaked = {pid: command for pid, command in leaked.items() if pid in still}
        if not leaked:
            return
        _terminate(sorted(leaked))
        lines = [
            f"[x] {len(leaked)} git credential helper process(es) started during the run "
            "were still running at the end (#5474):",
            *(f"    pid {pid}: {command}" for pid, command in sorted(leaked.items())),
            "    A test ran git against an ambient credential helper. Keep the "
            "GIT_CONFIG_SYSTEM isolation from tests/utils/credential_helpers.py, "
            "or configure a helper the test owns and stops.",
        ]
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line("\n".join(lines), red=True)
        else:
            sys.stderr.write("\n".join(lines) + "\n")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def register(config: pytest.Config) -> None:
    """Register the guard on the controller only; xdist workers skip it."""
    if hasattr(config, "workerinput") or config.pluginmanager.has_plugin(PLUGIN_NAME):
        return
    config.pluginmanager.register(CredentialHelperLeakGuard(), PLUGIN_NAME)


def pytest_configure(config: pytest.Config) -> None:
    """Entry point for ``-p tests.utils.credential_helpers``."""
    register(config)
