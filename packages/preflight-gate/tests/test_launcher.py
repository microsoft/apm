"""Launcher (R1): must never brick tool calls; fast path skips Python."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import SCRIPTS, Sandbox

LAUNCHER = "gate-launcher.sh"
HOOK_JSON = SCRIPTS.parent / "preflight-gate.json"


def install_bundle(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for path in SCRIPTS.iterdir():
        if path.is_file():
            shutil.copy2(path, dest / path.name)
    return dest


def launch(bundle: Path, stdin: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(bundle / LAUNCHER)],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )


def payload(command: str, cwd: Path) -> str:
    return json.dumps(
        {
            "sessionId": "s",
            "timestamp": 1,
            "cwd": str(cwd),
            "toolName": "bash",
            "toolArgs": {"command": command, "description": "d"},
        }
    )


def test_hook_json_shape() -> None:
    data = json.loads(HOOK_JSON.read_text(encoding="utf-8"))
    assert data["version"] == 1
    (entry,) = data["hooks"]["preToolUse"]
    assert entry["type"] == "command"
    assert entry["bash"].endswith("|| true")
    assert "${PLUGIN_ROOT}/.apm/hooks/scripts/gate-launcher.sh" in entry["bash"]
    assert entry["powershell"] == "exit 0"
    assert entry["timeoutSec"] == 15


def test_fast_path_never_starts_python(sandbox: Sandbox) -> None:
    bundle = install_bundle(sandbox.root / "bundle")
    marker = sandbox.root / "python-ran"
    (bundle / "gate.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n", encoding="utf-8")
    for command in ("ls -la", "cat README.md", "view file"):
        proc = launch(bundle, payload(command, sandbox.root))
        assert (proc.returncode, proc.stdout) == (0, "")
    assert not marker.exists()
    launch(bundle, payload("git status", sandbox.root))
    assert marker.exists()


def test_gated_call_without_receipt_is_denied_through_launcher(sandbox: Sandbox) -> None:
    bundle = install_bundle(sandbox.root / "bundle")
    proc = launch(bundle, payload("git push origin feature", sandbox.root))
    assert proc.returncode == 0
    (line,) = proc.stdout.splitlines()
    decision = json.loads(line)
    assert decision["permissionDecision"] == "deny"
    assert "no fresh green preflight receipt" in decision["permissionDecisionReason"]
    assert not (bundle / "__pycache__").exists()


@pytest.mark.parametrize("breakage", ["missing", "syntax", "crash"])
def test_broken_gate_fails_open(sandbox: Sandbox, breakage: str) -> None:
    bundle = install_bundle(sandbox.root / "bundle")
    gate = bundle / "gate.py"
    if breakage == "missing":
        gate.unlink()
    elif breakage == "syntax":
        gate.write_text("def broken(:\n", encoding="utf-8")
    else:
        gate.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    proc = launch(bundle, payload("git push", sandbox.root))
    assert (proc.returncode, proc.stdout) == (0, "")


def test_python_missing_fails_open(sandbox: Sandbox, workdir: Path) -> None:
    bundle = install_bundle(sandbox.root / "bundle")
    bin_dir = workdir / "bin"
    bin_dir.mkdir()
    for tool in ("cat", "dirname"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    env = {"PATH": str(bin_dir), "HOME": os.environ.get("HOME", "")}
    proc = launch(bundle, payload("git push", sandbox.root), env=env)
    assert (proc.returncode, proc.stdout) == (0, "")


def test_missing_launcher_fails_open_via_hook_command(workdir: Path) -> None:
    command = json.loads(HOOK_JSON.read_text(encoding="utf-8"))["hooks"]["preToolUse"][0]["bash"]
    resolved = command.replace("${PLUGIN_ROOT}", str(workdir / "nowhere"))
    proc = subprocess.run(
        ["bash", "-c", resolved],
        input=payload("git push", workdir),
        capture_output=True,
        text=True,
        check=False,
        cwd=workdir,
    )
    assert (proc.returncode, proc.stdout) == (0, "")
