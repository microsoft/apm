"""Installed-CLI contract: Ctrl+C stops the running lifecycle command script.

Lifecycle command scripts run in their own session, so a terminal Ctrl+C
reaches only ``apm``. An interrupted ``apm compile`` must stop its
``pre-compile`` script before exiting, so the script cannot keep writing
sources after the command has released its lifecycle lock.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.integration._fault_injection_kit import build_world, evidence

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(os.name == "nt", reason="POSIX process groups and signals"),
]

_SCRIPT = (
    "echo started > started.txt; sleep 6; "
    "echo late > .apm/instructions/late.instructions.md; echo done > finished.txt"
)


def test_interrupted_compile_stops_its_pre_compile_script(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path, packages=("alpha",), targets=("copilot",))
    manifest = world.project / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="ascii")
        + "lifecycle:\n  pre-compile:\n    - type: command\n"
        + f'      bash: "{_SCRIPT}"\n      timeoutSec: 60\n',
        encoding="ascii",
    )
    (world.project / ".apm/instructions").mkdir(parents=True)
    trust = world.apm("lifecycle", "trust")
    assert trust.returncode == 0, evidence(trust)

    process = subprocess.Popen(
        (str(apm_binary_path), "compile"),
        cwd=world.project,
        env=world.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    started = world.project / "started.txt"
    deadline = time.monotonic() + 120
    while not started.exists():
        assert process.poll() is None, process.communicate()[0]
        assert time.monotonic() < deadline, "pre-compile script never started"
        time.sleep(0.05)
    os.killpg(process.pid, signal.SIGINT)
    output, _ = process.communicate(timeout=60)
    assert process.returncode != 0, output

    time.sleep(8)
    assert not (world.project / "finished.txt").exists(), output
    assert not (world.project / ".apm/instructions/late.instructions.md").exists()
