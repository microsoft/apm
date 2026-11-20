"""Installed-CLI regression: a written lockfile version is never lowered (req-lk-002).

A version 2 lockfile records a git dependency resolved through a semver range
(the lockfile an earlier APM release commits, with no package content hashes).
After that dependency is uninstalled, every later rewrite of the same
lockfile keeps version 2 or later and never writes version 1. Version 4 still
follows its own rule (framed package content hashes).
"""

from __future__ import annotations

import os
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
    pytest.mark.promise("req-lk-002"),
]

_REMOTE = "https://github.com/apm-fixture-org/semver-tools"


def _skill_package(root: Path, name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    dump_yaml({"name": name, "version": "1.0.0"}, root / "apm.yml")
    skill = root / ".apm" / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"---\nname: {name}\ndescription: {name}\n---\n\nBody.\n", encoding="utf-8")


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


@pytest.mark.req("req-lk-002")
def test_lockfile_version_is_kept_after_the_v2_dependency_is_removed(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create("semver-tools")
    _skill_package(repository.worktree, "semver-tools")
    repositories.tag(repository, "v1.0.0", repositories.commit(repository, message="v1"))
    env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)

    project = isolated.work_root / "consumer"
    _skill_package(project / "pkg", "local-tools")
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./pkg", f"{_REMOTE}#^1.0.0"]},
        },
        project / "apm.yml",
    )

    _apm(apm_binary_path, project, env, "install")
    lock = load_yaml(project / "apm.lock.yaml")
    assert any(row.get("constraint") == "^1.0.0" for row in lock["dependencies"]), lock
    lock["lockfile_version"] = "2"
    for row in lock["dependencies"]:
        row.pop("content_hash", None)
    dump_yaml(lock, project / "apm.lock.yaml")

    _apm(apm_binary_path, project, env, "uninstall", "apm-fixture-org/semver-tools")
    assert load_yaml(project / "apm.yml")["dependencies"]["apm"] == ["./pkg"]
    assert "lockfile_version: '2'" in (project / "apm.lock.yaml").read_text(encoding="utf-8")

    _apm(apm_binary_path, project, env, "install")
    lock_text = (project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert "lockfile_version: '1'" not in lock_text
    assert "lockfile_version: '2'" in lock_text or "lockfile_version: '4'" in lock_text
