"""Installed-CLI regression: a converged ``apm install`` reports no change.

Promise (install summary, deployment owner): a second plain ``apm install`` on
a converged project changes no bytes, prints no "integrated" line for any
package, repeats no warning, and does not claim to have installed anything; the project root's own
``.apm/`` content is never counted as an installed APM dependency.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-install-outcome"),
]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(root: Path) -> Path:
    project = root / "consumer"
    package = project / "pkg"
    package.mkdir(parents=True)
    dump_yaml({"name": "pkg", "version": "1.0.0"}, package / "apm.yml")
    _write(
        package / ".apm" / "skills" / "demo" / "SKILL.md",
        "---\nname: demo\ndescription: Demo skill\n---\n\nBody.\n",
    )
    _write(
        package / ".apm" / "prompts" / "review.prompt.md",
        "---\ndescription: Review the change\nmode: agent\n---\n\nReview $ARGUMENTS.\n",
    )
    _write(
        package / ".apm" / "instructions" / "style.instructions.md",
        "---\napplyTo: '**'\n---\n\nKeep it short.\n",
    )
    _write(
        project / ".apm" / "instructions" / "local.instructions.md",
        "---\napplyTo: '**'\n---\n\nProject rule.\n",
    )
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude", "copilot"],
            "dependencies": {"apm": ["./pkg"]},
        },
        project / "apm.yml",
    )
    return project


def _bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _install(binary: Path, project: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        (str(binary), "install"),
        cwd=project,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    return output


def test_converged_install_reports_no_change(apm_binary_path: Path, tmp_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = _project(isolated.work_root)

    first = _install(apm_binary_path, project, env)
    assert "Installed 1 APM dependency" in first, first
    converged = _bytes(project)

    second = _install(apm_binary_path, project, env)

    assert _bytes(project) == converged
    assert "integrated" not in second, second
    assert "Installed" not in second, second
    assert "already up to date" in second, second
    assert "[!]" not in second, second
