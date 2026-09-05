"""Release diagnostics retain full coverage and work through xdist capture."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import load_workflow, shell_commands, workflow_job, workflow_step

pytestmark = pytest.mark.component

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/build-release.yml"
TEST_COMMAND = ["uv", "run", "pytest", "tests/unit", "tests/test_console.py"]
PARALLEL_ARGS = ["-n", "auto", "--dist", "worksteal"]
DIAGNOSTIC_ARGS = ["-vv", "--tb=short", "--show-capture=no", "--no-showlocals"]


def _assert_windows_diagnostics(workflow: dict) -> None:
    """Windows stays exhaustive, parallel, bounded, and fail-closed."""
    job = workflow_job(workflow, "build-and-test")
    linux = workflow_step(job, "Run unit tests")
    windows = workflow_step(job, "Run unit tests (Windows diagnostics)")
    assert linux["if"] == "matrix.platform != 'windows'"
    assert shell_commands(linux) == [TEST_COMMAND + PARALLEL_ARGS]
    assert windows["if"] == "matrix.platform == 'windows'"
    assert windows["timeout-minutes"] == 60
    assert shell_commands(windows) == [[*TEST_COMMAND, *PARALLEL_ARGS, *DIAGNOSTIC_ARGS]]
    assert windows["env"] == {**linux["env"], "PYTHONUNBUFFERED": "1"}
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
def test_xdist_reports_names_while_blocked_without_capture_or_locals(
    tmp_path: Path, phase: str
) -> None:
    """Observe live names on each OS before releasing a blocked test phase."""
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="ascii")
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import time\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "def wait_for_release():\n"
        "    Path('entered').touch()\n"
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
    ]
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTEST_ADDOPTS": "",
    }
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    output_path = tmp_path / "pytest.log"
    with (
        output_path.open("w", encoding="utf-8") as log,
        subprocess.Popen(
            command,
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process,
    ):
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                output = output_path.read_text(encoding="utf-8")
                if (
                    (tmp_path / "entered").exists()
                    and "test_probe.py::test_diagnostic_probe" in output
                ) or process.poll() is not None:
                    break
                time.sleep(0.05)
            assert (tmp_path / "entered").exists(), output
            assert "test_probe.py::test_diagnostic_probe" in output, output
            assert process.poll() is None
            if phase == "teardown":
                assert "FAILED" in output
        finally:
            (tmp_path / "release").touch()
            try:
                process.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
                raise
    output = output_path.read_text(encoding="utf-8")
    assert process.returncode == 1, output
    assert "FAILED" in output and "test_diagnostic_probe" in output
    assert "1 failed, 1 passed" in output
    assert "DO_NOT_DUMP_PROBE_LOCAL" not in output
    assert "DO_NOT_DUMP_PROBE_CAPTURE" not in output
