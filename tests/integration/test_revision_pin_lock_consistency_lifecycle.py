"""A full-SHA manifest pin selects that exact commit across install and audit."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

KIT_URL = "https://github.com/test/kit.git"
SKILL = ".apm/skills/alpha/SKILL.md"
DEPLOYED = Path(".claude/skills/alpha/SKILL.md")


def _skill(body: str) -> str:
    return f"---\nname: alpha\ndescription: alpha skill\n---\n{body}\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("ascii"))


def test_sha_pinned_manifest_installs_pinned_commit_over_divergent_lock_row(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "sha-pin", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
    source = isolated.package_root / "kit"
    _write(source / "apm.yml", "name: kit\nversion: 1.0.0\ndescription: kit\n")
    _write(source / SKILL, _skill("pinned"))
    repository = factory.create("kit", source_tree=source)
    pinned = factory.commit(repository, message="pinned")
    _write(repository.worktree / SKILL, _skill("later"))
    later = factory.commit(repository, message="later")
    env = factory.url_rewrite_subprocess_env(repository, KIT_URL)

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": [f"test/kit#{pinned.sha}"], "mcp": []},
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def run(*args: str):
        return runner.run(args, scenario_id="sha-pin", cwd=project, env=env)

    first = run("install", "--no-policy")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "pinned" in (project / DEPLOYED).read_text(encoding="ascii")

    # Point the lock row at another commit while keeping resolved_ref on the pin.
    lockfile = project / "apm.lock.yaml"
    lock = load_yaml(lockfile)
    (row,) = lock["dependencies"]
    assert row["resolved_ref"] == pinned.sha
    row["resolved_commit"] = later.sha
    row.pop("content_hash", None)
    dump_yaml(lock, lockfile)

    audit = run("audit", "--ci", "--no-policy")
    assert audit.returncode != 0
    assert f"lockfile resolved_commit '{later.sha}'" in audit.stdout + audit.stderr

    frozen = run("install", "--no-policy", "--frozen")
    assert frozen.returncode != 0
    assert "pinned" in (project / DEPLOYED).read_text(encoding="ascii")

    reinstall = run("install", "--no-policy")
    assert reinstall.returncode == 0, reinstall.stdout + reinstall.stderr
    assert "pinned" in (project / DEPLOYED).read_text(encoding="ascii")
    (row,) = load_yaml(lockfile)["dependencies"]
    assert row["resolved_commit"] == pinned.sha
    assert row["resolved_ref"] == pinned.sha

    clean_audit = run("audit", "--ci", "--no-policy")
    assert clean_audit.returncode == 0, clean_audit.stdout + clean_audit.stderr
