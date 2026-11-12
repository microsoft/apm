"""Installed-CLI contracts for cold restore of committed skills (#3391).

A fresh clone on a machine without deployment receipts restores committed
``claude_skill`` and ``skill_bundle`` output that is byte-identical to what
the install would write: exit 0, bytes and lockfile intact, receipts recorded.
A locally edited member keeps the #3372 fail-closed path: nothing is
overwritten or deleted and the command exits nonzero with guidance.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_TARGET_ROOTS = (".agents/skills", ".claude/skills")
_REFUSAL = "no trustworthy local deployment baseline"
_GUIDANCE = "Move the conflicting file aside and retry"


@dataclass(frozen=True)
class _Restore:
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    runner: ApmLifecycleRunner
    clone: Path
    skills: tuple[str, ...]


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=60
    ).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _skill_source(root: Path, kind: str) -> tuple[str, ...]:
    """Write a package with no apm.yml, the way public skill repositories ship."""
    if kind == "claude_skill":
        _write(root / "SKILL.md", "---\nname: solo\ndescription: Solo\n---\nSee guide.md.\n")
        _write(root / "references/guide.md", "Guide.\n")
        return (root.name,)
    _write(
        root / "skills/alpha/SKILL.md",
        "---\nname: alpha\ndescription: Alpha\n---\nUse [beta](../beta/notes.md).\n",
    )
    _write(root / "skills/alpha/asset.md", "Alpha asset.\n")
    _write(root / "skills/beta/SKILL.md", "---\nname: beta\ndescription: Beta\n---\nBeta.\n")
    _write(root / "skills/beta/notes.md", "Beta notes.\n")
    return ("alpha", "beta")


def _evict_machine_state(isolated: IsolatedApmEnvironment) -> None:
    """Emulate a fresh CI runner: no receipts and no download cache."""
    for receipts in (isolated.home / ".apm").glob("deployment-receipts*"):
        shutil.rmtree(receipts)
    if isolated.cache_root.exists():
        shutil.rmtree(isolated.cache_root)
    isolated.cache_root.mkdir(parents=True)


def _committed_restore(tmp_path: Path, apm_binary_path: Path, kind: str) -> _Restore:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    source = isolated.package_root / "skills-source"
    skills = _skill_source(source, kind)
    repository = repositories.create("skills-source", source_tree=source)
    commit = repositories.commit(repository, message="publish skills")
    remote_url = "https://github.com/test/skills-source.git"
    env = repositories.url_rewrite_subprocess_env(repository, remote_url)

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude", "copilot"],
            "dependencies": {"apm": [{"git": remote_url, "ref": commit.sha}]},
        },
        project / "apm.yml",
    )
    _write(project / ".gitignore", "apm_modules/\n")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    first = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    lock = (project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert f"package_type: {kind}" in lock, lock
    _git(project, env, "init", "--initial-branch=main")
    _git(project, env, "config", "user.email", "test@example.com")
    _git(project, env, "config", "user.name", "APM Test")
    _git(project, env, "add", "--all")
    _git(project, env, "commit", "-m", "commit deployed skills")

    clone = isolated.work_root / "fresh-clone"
    _git(isolated.work_root, env, "clone", "--quiet", str(project), str(clone))
    _evict_machine_state(isolated)
    return _Restore(isolated, env, runner, clone, skills)


def _receipt_destinations(isolated: IsolatedApmEnvironment) -> set[str]:
    destinations: set[str] = set()
    for receipt in (isolated.home / ".apm").glob("deployment-receipts*/*.json"):
        destinations.update(
            json.loads(receipt.read_text(encoding="utf-8"))["destinations"].values()
        )
    return destinations


@pytest.mark.parametrize("frozen", [False, True], ids=["install", "frozen"])
@pytest.mark.parametrize("kind", ["claude_skill", "skill_bundle"])
def test_fresh_clone_adopts_unchanged_committed_skills(
    tmp_path: Path, apm_binary_path: Path, kind: str, frozen: bool
) -> None:
    """Cold restore keeps identical committed skills and records local receipts."""
    restore = _committed_restore(tmp_path, apm_binary_path, kind)
    clone = restore.clone
    roots = tuple(clone / root for root in _TARGET_ROOTS)
    before = [ArtifactSnapshot.capture(root) for root in roots]
    lock_before = (clone / "apm.lock.yaml").read_bytes()
    if kind == "skill_bundle":
        rendered = (roots[0] / "alpha/SKILL.md").read_text(encoding="ascii")
        assert "](../beta/notes.md)" not in rendered, "fixture must exercise link rendering"
    assert not _receipt_destinations(restore.isolated)

    command = ("install", "--no-policy", *(("--frozen",) if frozen else ()))
    for _ in range(2):
        result = restore.runner.run(command, cwd=clone, env=restore.env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _REFUSAL not in result.stdout + result.stderr
        assert "stale" not in result.stdout.lower()
        assert _git(clone, restore.env, "status", "--porcelain") == ""
        assert (clone / "apm.lock.yaml").read_bytes() == lock_before
        for snapshot, root in zip(before, roots, strict=True):
            assert_unchanged(snapshot, ArtifactSnapshot.capture(root))

    recorded = _receipt_destinations(restore.isolated)
    for root in roots:
        for skill in restore.skills:
            anchor = (root / skill / "SKILL.md").resolve().as_posix()
            assert anchor in recorded, sorted(recorded)
    audit = restore.runner.run(("audit", "--ci", "--no-policy"), cwd=clone, env=restore.env)
    assert audit.returncode == 0, audit.stdout + audit.stderr


@pytest.mark.parametrize("frozen", [False, True], ids=["install", "frozen"])
@pytest.mark.parametrize("kind", ["claude_skill", "skill_bundle"])
def test_fresh_clone_with_edited_skill_fails_closed(
    tmp_path: Path, apm_binary_path: Path, kind: str, frozen: bool
) -> None:
    """A differing member is neither overwritten nor deleted; install exits nonzero."""
    restore = _committed_restore(tmp_path, apm_binary_path, kind)
    clone = restore.clone
    edited = clone / ".claude/skills" / restore.skills[0] / "SKILL.md"
    edited.write_text("Local edits.\n", encoding="ascii")
    roots = tuple(clone / root for root in _TARGET_ROOTS)
    before = [ArtifactSnapshot.capture(root) for root in roots]
    lock_before = (clone / "apm.lock.yaml").read_bytes()

    command = ("install", "--no-policy", *(("--frozen",) if frozen else ()))
    for _ in range(2):
        result = restore.runner.run(command, cwd=clone, env=restore.env)
        assert result.returncode != 0, result.stdout + result.stderr
        output = " ".join((result.stdout + result.stderr).split())
        assert _REFUSAL in output, output
        assert _GUIDANCE in output, output
        assert "stale files cleaned" not in result.stdout
        assert edited.read_text(encoding="ascii") == "Local edits.\n"
        assert (clone / "apm.lock.yaml").read_bytes() == lock_before
        for snapshot, root in zip(before, roots, strict=True):
            assert_unchanged(snapshot, ArtifactSnapshot.capture(root))
