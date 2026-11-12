"""Installed-CLI regression for #3369: root-only hooks audit clean after install.

A consumer whose only root ``.apm`` primitives are hooks deploys nothing
standalone, so the lockfile records no ``local_deployed_files`` and no
virtual self-entry. Install still merges the root hook into the shared
Claude hook configuration next to the dependency's hook; the audit replay
must do the same or it reports false drift on both shared files.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_SIDECAR = ".claude/apm-hooks.json"
_SETTINGS = ".claude/settings.json"


def _run(apm_binary_path: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(apm_binary_path), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _write_issue_fixture(project: Path) -> None:
    """Write the four-file reproduction from #3369."""
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: consumer\n"
        "version: 1.0.0\n"
        "targets: [claude]\n"
        "includes: auto\n"
        "dependencies:\n"
        "  apm:\n"
        "    - ./package\n",
        encoding="utf-8",
    )
    _write_json(
        project / ".apm" / "hooks" / "root.json",
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo root"}]}]},
    )
    package = project / "package"
    package.mkdir()
    (package / "apm.yml").write_text("name: package\nversion: 1.0.0\n", encoding="utf-8")
    _write_json(
        package / ".apm" / "hooks" / "dependency.json",
        {"PostToolUse": [{"hooks": [{"type": "command", "command": "echo dependency"}]}]},
    )


def _audit(apm_binary_path: Path, project: Path) -> tuple[int, str]:
    result = _run(apm_binary_path, project, "audit", "--ci", "--no-policy")
    return result.returncode, result.stdout + result.stderr


def _drifted(output: str, path: str) -> bool:
    return f"modified: {path}" in output


def _commands(project: Path, event: str) -> list[str]:
    settings = json.loads((project / _SETTINGS).read_text(encoding="utf-8"))
    return [
        handler["command"]
        for entry in settings["hooks"][event]
        for handler in entry.get("hooks", [])
    ]


def test_root_only_hooks_with_hook_dependency_audit_clean_after_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = tmp_path / "consumer"
    _write_issue_fixture(project)

    install = _run(apm_binary_path, project, "install")
    assert install.returncode == 0, install.stderr or install.stdout

    # Install merges both hooks into the shared Claude config, while the
    # lockfile tracks no standalone root files (the precondition of #3369).
    assert _commands(project, "PreToolUse") == ["echo root"]
    assert _commands(project, "PostToolUse") == ["echo dependency"]
    lock = load_yaml(project / "apm.lock.yaml")
    assert not lock.get("local_deployed_files")
    lock_bytes = (project / "apm.lock.yaml").read_bytes()

    returncode, output = _audit(apm_binary_path, project)
    assert returncode == 0, output
    assert "Drift detected" not in output
    assert not _drifted(output, _SIDECAR)
    assert not _drifted(output, _SETTINGS)
    # Audit replay must not rewrite the lockfile.
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes

    # A meaningful edit to the APM-owned root hook is still reported as drift.
    settings_path = project / _SETTINGS
    original_settings = settings_path.read_text(encoding="utf-8")
    settings_path.write_text(
        original_settings.replace("echo root", "echo tampered"), encoding="utf-8"
    )
    returncode, output = _audit(apm_binary_path, project)
    assert returncode == 1, output
    assert _drifted(output, _SETTINGS), output
    settings_path.write_text(original_settings, encoding="utf-8")

    sidecar_path = project / _SIDECAR
    original_sidecar = sidecar_path.read_text(encoding="utf-8")
    sidecar_path.write_text(
        original_sidecar.replace("echo root", "echo tampered"), encoding="utf-8"
    )
    returncode, output = _audit(apm_binary_path, project)
    assert returncode == 1, output
    assert _drifted(output, _SIDECAR), output
