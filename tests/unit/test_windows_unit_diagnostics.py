"""Release diagnostics retain full coverage and work through xdist capture."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch

import pytest

from tests import pytest_hang_diagnostics as diagnostics
from tests.workflow_contracts import load_workflow, shell_commands, workflow_job, workflow_step

pytestmark = pytest.mark.component

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/build-release.yml"
TEST_COMMAND = ["uv", "run", "pytest", "tests/unit", "tests/test_console.py"]
PARALLEL_ARGS = ["-n", "auto", "--dist", "worksteal"]
DIAGNOSTIC_ARGS = ["-vv", "--tb=short", "--show-capture=no", "--no-showlocals"]
PLUGIN_ARGS = ["-p", "no:faulthandler", "-p", "tests.pytest_hang_diagnostics"]


def _assert_windows_diagnostics(workflow: dict) -> None:
    """Windows stays exhaustive, parallel, bounded, and fail-closed."""
    job = workflow_job(workflow, "build-and-test")
    linux = workflow_step(job, "Run unit tests")
    windows = workflow_step(job, "Run unit tests (Windows diagnostics)")
    assert linux["if"] == "matrix.platform != 'windows'"
    assert shell_commands(linux) == [TEST_COMMAND + PARALLEL_ARGS]
    assert windows["if"] == "matrix.platform == 'windows'"
    assert windows["timeout-minutes"] == 60
    assert shell_commands(windows) == [
        [
            "uv",
            "run",
            "python",
            "-m",
            *TEST_COMMAND[2:],
            *PARALLEL_ARGS,
            *DIAGNOSTIC_ARGS,
            *PLUGIN_ARGS,
        ]
    ]
    assert windows["env"] == linux["env"]
    for node in (job, windows):
        assert not node.get("continue-on-error", False)


def test_windows_release_unit_diagnostics_contract() -> None:
    """Diagnostics must not change test selection or turn failures green."""
    _assert_windows_diagnostics(load_workflow(WORKFLOW))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("run", "echo pytest"),
        ("timeout-minutes", 10),
        ("continue-on-error", True),
        ("if", "matrix.platform == 'linux'"),
    ],
)
def test_windows_diagnostics_contract_rejects_mutations(key: str, value: object) -> None:
    """A success-shaped replacement or changed safety bound is rejected."""
    workflow = deepcopy(load_workflow(WORKFLOW))
    step = workflow_step(
        workflow_job(workflow, "build-and-test"), "Run unit tests (Windows diagnostics)"
    )
    step[key] = value
    with pytest.raises(AssertionError):
        _assert_windows_diagnostics(workflow)


@pytest.mark.windows_compat
@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_xdist_watchdog_reports_stacks_without_capture_or_locals(
    tmp_path: Path, phase: str
) -> None:
    """Exercise the installed watchdog on each OS, not a platform-name mock."""
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="ascii")
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import time\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "def wait_for_release():\n"
        "    deadline = time.monotonic() + 30\n"
        "    while not Path('release').exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.05)\n"
        "@pytest.fixture\n"
        "def stalled_fixture():\n"
        f"    if {phase!r} == 'setup':\n"
        "        wait_for_release()\n"
        "    yield\n"
        f"    if {phase!r} == 'teardown':\n"
        "        wait_for_release()\n"
        "def test_diagnostic_probe(stalled_fixture):\n"
        "    private_value = 'DO_NOT_DUMP_PROBE_LOCAL'\n"
        "    print('DO_NOT_DUMP_PROBE_CAPTURE')\n"
        f"    if {phase!r} == 'call':\n"
        "        wait_for_release()\n"
        "    assert False, 'intentional diagnostic probe failure'\n"
        "def test_suite_continues():\n"
        "    assert 2 + 2 == 4\n",
        encoding="ascii",
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        *PLUGIN_ARGS,
        "-c",
        str(config),
        "--confcutdir",
        str(tmp_path),
        str(probe),
        "-n",
        "2",
        "--dist",
        "worksteal",
        *DIAGNOSTIC_ARGS,
        "-o",
        "hang_dump_interval=0.1",
    ]
    env = {
        **os.environ,
        "PYTHONPATH": str(WORKFLOW.parents[2]),
        "PYTEST_ADDOPTS": "",
    }
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    stderr_path = tmp_path / "stacks.log"
    stalled_function = "test_diagnostic_probe" if phase == "call" else "stalled_fixture"
    with (
        stderr_path.open("w", encoding="utf-8") as stderr,
        subprocess.Popen(
            command,
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        ) as process,
    ):
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                stacks = stderr_path.read_text(encoding="utf-8")
                if f"in {stalled_function}" in stacks or process.poll() is not None:
                    break
                time.sleep(0.05)
            # Read the native descriptor while the fixture/test is still blocked,
            # before pytest could replay captured output or finish the session.
            assert f"in {stalled_function}" in stacks, stacks
            assert process.poll() is None
        finally:
            (tmp_path / "release").touch()
            try:
                stdout, _ = process.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
                raise
    stacks = stderr_path.read_text(encoding="utf-8")
    output = stdout + stacks
    assert process.returncode == 1, output
    assert "FAILED" in stdout and "test_diagnostic_probe" in stdout
    assert "1 failed, 1 passed" in stdout
    assert "most recent call first" in stacks
    assert "DO_NOT_DUMP_PROBE_LOCAL" not in output
    assert "DO_NOT_DUMP_PROBE_CAPTURE" not in output


@pytest.mark.windows_compat
def test_diagnostics_reject_builtin_timer_interference(tmp_path: Path) -> None:
    """Leaving pytest's cancellation hook loaded must fail before tests run."""
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="ascii")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.pytest_hang_diagnostics",
            "-c",
            str(config),
            "--confcutdir",
            str(tmp_path),
            str(tmp_path),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(WORKFLOW.parents[2]),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_ADDOPTS": "",
        },
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "hang diagnostics requires -p no:faulthandler" in result.stderr


def test_watchdog_cleanup_is_repeatable_and_closes_descriptors() -> None:
    """Repeated sessions release their reporter and descriptor exactly once."""
    config = Mock(spec=pytest.Config)
    config.stash = pytest.Stash()
    for _ in range(3):
        fd = os.dup(sys.__stderr__.fileno())
        stop = Event()
        thread = Thread(target=stop.wait, daemon=True)
        thread.start()
        config.stash[diagnostics._WATCHDOG] = (stop, thread, fd)
        with patch.object(diagnostics.faulthandler, "disable"):
            diagnostics.pytest_unconfigure(config)
            diagnostics.pytest_unconfigure(config)
        assert not thread.is_alive()
        assert diagnostics._WATCHDOG not in config.stash
        with pytest.raises(OSError):
            os.fstat(fd)


def test_watchdog_shutdown_timeout_fails_without_reusing_live_descriptor() -> None:
    """A blocked reporter cannot make cleanup hang or close a live output fd."""
    config = Mock(spec=pytest.Config)
    config.stash = pytest.Stash()
    stop = Event()
    thread = Mock(spec=Thread)
    thread.is_alive.return_value = True
    config.stash[diagnostics._WATCHDOG] = (stop, thread, 123)
    with patch.object(diagnostics.os, "close") as close:
        with pytest.raises(RuntimeError, match="reporter did not stop"):
            diagnostics.pytest_unconfigure(config)
    assert stop.is_set()
    thread.join.assert_called_once_with(timeout=1)
    close.assert_not_called()
