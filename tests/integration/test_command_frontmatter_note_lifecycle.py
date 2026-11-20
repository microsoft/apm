"""Installed-CLI regression: a dropped command frontmatter key is a verbose note.

Promise (command integrator, ``src/apm_cli/integration/command_integrator.py``):
when a target's command format cannot carry a frontmatter key (for example
``mode:`` for Claude commands), the key is dropped and reported as a verbose
note, never as a ``[!]`` warning on a default install or ``install --frozen``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics"),
]

_DROPPED = "were dropped: mode"


def _apm(binary: Path, project: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        (str(binary), *args),
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


def test_dropped_command_key_is_a_verbose_note(apm_binary_path: Path, tmp_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    prompt = project / "pkg" / ".apm" / "prompts" / "review.prompt.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text(
        "---\ndescription: Review the change\nmode: agent\n---\n\nReview it.\n", encoding="utf-8"
    )
    dump_yaml({"name": "pkg", "version": "1.0.0"}, project / "pkg" / "apm.yml")
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./pkg"]},
        },
        project / "apm.yml",
    )

    first = _apm(apm_binary_path, project, env, "install")
    assert (project / ".claude" / "commands" / "review.md").is_file(), first
    assert "[!]" not in first, first
    assert _DROPPED not in first, first

    shutil.rmtree(project / "apm_modules")
    shutil.rmtree(project / ".claude")
    frozen = _apm(apm_binary_path, project, env, "install", "--frozen")
    assert "[!]" not in frozen, frozen

    shutil.rmtree(project / ".claude")
    verbose = _apm(apm_binary_path, project, env, "install", "--verbose")
    assert _DROPPED in verbose, verbose
