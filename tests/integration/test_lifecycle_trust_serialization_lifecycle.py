"""Installed-CLI regressions: trust-store writes share the lifecycle serialization owner.

``apm lifecycle trust`` and ``apm lifecycle untrust`` read, modify and write
the script trust store under ``$APM_HOME``. Every command that writes APM
lifecycle state serializes through ``install/locking.py``. A second writer that
holds that canonical lifecycle lock across its own read-modify-write of the
store must therefore never interleave with the trust commands: the record a
command reports as written (or revoked) must still be in that state once both
writers finish.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_TIMEOUT_SECONDS = 120.0
_WAITING_MARKER = "Another APM operation is running"

# A concurrent lifecycle-state writer: it holds the canonical lifecycle lock,
# snapshots the trust store, waits for the test to release it, then writes its
# own record on top of that snapshot.
_LOCK_HOLDER = """
import json, sys
from pathlib import Path
from apm_cli.core.scope import get_apm_home
from apm_cli.core.script_trust import script_file_fingerprint
from apm_cli.install.locking import lifecycle_operation

manifest = Path(sys.argv[1])
store = get_apm_home() / "scripts-trust.json"
with lifecycle_operation():
    snapshot = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    projects = dict(snapshot.get("projects", {}))
    print("LOCKED", flush=True)
    sys.stdin.readline()
    projects[str(manifest.resolve())] = script_file_fingerprint(manifest)
    store.write_text(json.dumps({"version": 1, "projects": projects}), encoding="utf-8")
print("RELEASED", flush=True)
"""


@dataclass(frozen=True)
class _Workspace:
    environment: dict[str, str]
    store: Path
    holder_project: Path
    command_project: Path


class _OutputPump:
    """Collect a child's stdout without blocking the test thread."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.lines: list[str] = []
        self._thread = threading.Thread(target=self._read, args=(process,), daemon=True)
        self._thread.start()

    def _read(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self.lines.append(line)

    def text(self) -> str:
        return "".join(self.lines)

    def join(self) -> None:
        self._thread.join(timeout=_TIMEOUT_SECONDS)


def _project(root: Path, name: str) -> Path:
    project = root / name
    project.mkdir(parents=True)
    manifest = project / "apm.yml"
    dump_yaml(
        {
            "name": name,
            "version": "1.0.0",
            "lifecycle": {"post-install": [{"run": f"echo {name}"}]},
        },
        manifest,
    )
    return project


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[_Workspace]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    environment = isolated.subprocess_env(overrides={"PYTHONUNBUFFERED": "1"})
    yield _Workspace(
        environment=environment,
        store=isolated.config_root / "scripts-trust.json",
        holder_project=_project(isolated.work_root, "holder"),
        command_project=_project(isolated.work_root, "command"),
    )


def _records(store: Path) -> dict[str, str]:
    return dict(json.loads(store.read_text(encoding="utf-8"))["projects"])


def _run_cli(binary: Path, workspace: _Workspace, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(binary), *args),
        cwd=workspace.command_project,
        env=workspace.environment,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


def _race_with_lock_holder(binary: Path, workspace: _Workspace, verb: str) -> str:
    """Run ``apm lifecycle <verb>`` while another writer holds the lifecycle lock."""
    holder = subprocess.Popen(
        (sys.executable, "-c", _LOCK_HOLDER, str(workspace.holder_project / "apm.yml")),
        env=workspace.environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    command: subprocess.Popen[str] | None = None
    try:
        assert holder.stdout is not None and holder.stdin is not None
        assert holder.stdout.readline().strip() == "LOCKED"

        command = subprocess.Popen(
            (str(binary), "lifecycle", verb),
            cwd=workspace.command_project,
            env=workspace.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        pump = _OutputPump(command)
        deadline = time.monotonic() + _TIMEOUT_SECONDS
        # Proceed once the command either finished or reported it is waiting.
        while command.poll() is None and _WAITING_MARKER not in pump.text():
            assert time.monotonic() < deadline, pump.text()
            time.sleep(0.05)

        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "RELEASED"
        assert holder.wait(timeout=_TIMEOUT_SECONDS) == 0

        assert command.wait(timeout=_TIMEOUT_SECONDS) == 0, pump.text()
        pump.join()
        return pump.text()
    finally:
        for process in (holder, command):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=_TIMEOUT_SECONDS)


def test_concurrent_lifecycle_trust_keeps_every_trust_record(
    apm_binary_path: Path, workspace: _Workspace
) -> None:
    output = _race_with_lock_holder(apm_binary_path, workspace, "trust")

    assert "Trusted apm.yml lifecycle:" in output
    records = _records(workspace.store)
    assert str((workspace.holder_project / "apm.yml").resolve()) in records
    assert str((workspace.command_project / "apm.yml").resolve()) in records, (
        "the trust record apm lifecycle trust reported must be present after a "
        f"concurrent lifecycle writer:\n{output}"
    )
    assert _WAITING_MARKER in output


def test_concurrent_lifecycle_untrust_keeps_the_revocation(
    apm_binary_path: Path, workspace: _Workspace
) -> None:
    trusted = _run_cli(apm_binary_path, workspace, "lifecycle", "trust")
    assert trusted.returncode == 0, trusted.stdout + trusted.stderr
    command_key = str((workspace.command_project / "apm.yml").resolve())
    assert command_key in _records(workspace.store)

    output = _race_with_lock_holder(apm_binary_path, workspace, "untrust")

    assert "Revoked trust for apm.yml lifecycle:" in output
    records = _records(workspace.store)
    assert str((workspace.holder_project / "apm.yml").resolve()) in records
    assert command_key not in records, (
        "the revocation apm lifecycle untrust reported must hold after a concurrent "
        f"lifecycle writer:\n{output}"
    )
    assert _WAITING_MARKER in output
