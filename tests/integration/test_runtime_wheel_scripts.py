"""Verify runtime setup assets through an installed wheel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _venv_python(venv: Path) -> Path:
    posix = venv / "bin" / "python"
    return posix if posix.exists() else venv / "Scripts" / "python.exe"


def _run(command: list[str], *, cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_installed_wheel_loads_all_runtime_scripts(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build and install the wheel")

    wheel_dir = tmp_path / "wheel"
    outside_repo = tmp_path / "outside-repo"
    wheel_dir.mkdir()
    outside_repo.mkdir()
    _run([uv, "build", "--wheel", "--out-dir", str(wheel_dir)], cwd=_repo_root())

    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1

    venv = tmp_path / "venv"
    _run([uv, "venv", "--python", sys.executable, str(venv)], cwd=outside_repo)
    python = _venv_python(venv)
    _run([uv, "pip", "install", "--python", str(python), str(wheels[0])], cwd=outside_repo)

    probe = """
import json
from pathlib import Path
from unittest.mock import PropertyMock, patch

import apm_cli.runtime.manager as manager_module
from apm_cli.runtime.manager import RuntimeManager
from apm_cli.runtime.registry import runtime_descriptors

manager = RuntimeManager()
script_names = [
    f"{descriptor.setup_script}{extension}"
    for descriptor in runtime_descriptors()
    for extension in (".sh", ".ps1")
]
script_names.extend(("setup-common.sh", "setup-common.ps1"))
scripts = {name: manager.get_embedded_script(name) for name in script_names}
with patch.object(RuntimeManager, "_is_windows", new_callable=PropertyMock, return_value=False):
    token_helper = manager.get_token_helper_script()

print(json.dumps({
    "manager_file": manager_module.__file__,
    "script_names": sorted(scripts),
    "scripts_nonempty": all(scripts.values()),
    "token_helper_nonempty": bool(token_helper),
}))
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    result = subprocess.run(
        [str(python), "-c", probe],
        cwd=outside_repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    loaded = json.loads(result.stdout)
    expected = sorted(
        [
            "setup-codex.ps1",
            "setup-codex.sh",
            "setup-common.ps1",
            "setup-common.sh",
            "setup-copilot.ps1",
            "setup-copilot.sh",
            "setup-gemini.ps1",
            "setup-gemini.sh",
            "setup-llm.ps1",
            "setup-llm.sh",
        ]
    )
    assert loaded["script_names"] == expected
    assert loaded["scripts_nonempty"] is True
    assert loaded["token_helper_nonempty"] is True
    assert venv in Path(loaded["manager_file"]).parents
