"""Installed-CLI regression: the unpinned-dependency hint is correct and actionable.

Promise (install finalize owner, ``src/apm_cli/install/phases/finalize.py``):
a user is warned only about dependencies they can pin. An unpinned transitive
dependency is chosen by the package that declares it, so a default install
prints nothing about it (#4183); ``--verbose`` names the declaring package and
the exact commit pin, which works for untagged repositories, and following it
clears the note (RA-003).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics"),
]

_PRIMARY = "https://github.com/apm-fixture-org/pinhint-primary"
_SECONDARY = "https://github.com/apm-fixture-org/pinhint-secondary"
_INSTALL = ("install", "--target", "claude", "--parallel-downloads", "0")


def _package(root: Path, name: str, dependencies: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {"name": name, "version": "1.0.0"}
    if dependencies:
        manifest["dependencies"] = {"apm": dependencies}
    dump_yaml(manifest, root / "apm.yml")
    skill = root / ".apm" / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"---\nname: {name}\ndescription: {name}\n---\n\nBody.\n", encoding="utf-8")


def _apm(binary: Path, project: Path, env: dict[str, str], *args: str):
    return subprocess.run(
        (str(binary), *args),
        cwd=project,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_unpinned_transitive_hint_names_a_working_pin(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    secondary = repositories.create("pinhint-secondary")
    _package(secondary.worktree, "secondary", [])
    secondary_commit = repositories.commit(secondary, message="secondary").sha
    primary = repositories.create("pinhint-primary")
    _package(primary.worktree, "primary", [_SECONDARY])
    repositories.tag(primary, "v1.0.0", repositories.commit(primary, message="primary"))
    env = repositories.url_rewrite_subprocess_env_many(
        ((primary, _PRIMARY), (secondary, _SECONDARY))
    )
    project = isolated.work_root / "consumer"
    _package(project, "consumer", [f"{_PRIMARY}#v1.0.0"])

    first = _apm(apm_binary_path, project, env, *_INSTALL)
    output = first.stdout + first.stderr
    assert first.returncode == 0, output
    assert "unpinned" not in output, output

    shutil.rmtree(project / "apm_modules")
    verbose = _apm(apm_binary_path, project, env, *_INSTALL, "--verbose")
    output = verbose.stdout + verbose.stderr
    assert verbose.returncode == 0, output
    note = next(line for line in output.splitlines() if "transitive dependency unpinned" in line)
    assert "declared by" in note and "pinhint-primary" in note, output
    pins = re.findall(r"(\S+#[0-9a-f]{40})\b", note)
    assert pins == [f"apm-fixture-org/pinhint-secondary#{secondary_commit}"], output

    manifest = load_yaml(project / "apm.yml")
    manifest["dependencies"]["apm"].append(pins[0])
    dump_yaml(manifest, project / "apm.yml")
    shutil.rmtree(project / "apm_modules")
    second = _apm(apm_binary_path, project, env, *_INSTALL, "--verbose")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "unpinned" not in second.stdout + second.stderr
