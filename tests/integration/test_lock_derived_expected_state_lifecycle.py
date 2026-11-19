"""Installed-CLI contracts: install derives expected state from the lock (#3883).

A fresh clone without deployment receipts whose ``apm.yml`` ref was bumped
(the Renovate flow) adopts committed skill output that is byte-identical to
what the currently locked version renders, then installs the new version. A
hand-edited committed file still fails closed: nothing is overwritten.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_ROOTS = (".agents/skills", ".claude/skills")
_REFUSAL = "no trustworthy local deployment baseline"
_REMOTE = "https://github.com/test/skills-source.git"


@dataclass(frozen=True)
class _BumpedClone:
    env: dict[str, str]
    runner: ApmLifecycleRunner
    clone: Path
    v2_sha: str


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=60
    ).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _alpha(body: str) -> str:
    return f"---\nname: alpha\ndescription: Alpha\n---\n{body} [tools](../beta/references/x.md).\n"


def _bumped_clone(tmp_path: Path, apm_binary_path: Path) -> _BumpedClone:
    """Commit v1 output, clone it fresh, evict receipts, and bump apm.yml to v2."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    source = isolated.package_root / "skills-source"
    _write(source / "skills/alpha/SKILL.md", _alpha("Use"))
    _write(source / "skills/beta/SKILL.md", "---\nname: beta\ndescription: Beta\n---\nBeta.\n")
    _write(source / "skills/beta/references/x.md", "Tools.\n")
    repository = repositories.create("skills-source", source_tree=source)
    v1 = repositories.commit(repository, message="v1")
    repositories.tag(repository, "v1", v1)
    _write(repository.worktree / "skills/alpha/SKILL.md", _alpha("Now use"))
    v2 = repositories.commit(repository, message="v2")
    repositories.tag(repository, "v2", v2)
    env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude", "copilot"],
            "dependencies": {"apm": [{"git": _REMOTE, "ref": "v1"}]},
        },
        project / "apm.yml",
    )
    _write(project / ".gitignore", "apm_modules/\n")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    first = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    rendered = (project / ".claude/skills/alpha/SKILL.md").read_text(encoding="ascii")
    assert "](../beta/references/x.md)" not in rendered, "fixture must exercise link rendering"
    _git(project, env, "init", "--initial-branch=main")
    _git(project, env, "-c", "user.email=t@example.com", "-c", "user.name=T", "add", "--all")
    _git(project, env, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "v1")

    clone = isolated.work_root / "fresh-clone"
    _git(isolated.work_root, env, "clone", "--quiet", str(project), str(clone))
    for receipts in (isolated.home / ".apm").glob("deployment-receipts*"):
        shutil.rmtree(receipts)
    shutil.rmtree(isolated.cache_root, ignore_errors=True)
    isolated.cache_root.mkdir(parents=True)
    manifest = clone / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("ref: v1", "ref: v2"), encoding="utf-8"
    )
    return _BumpedClone(env, runner, clone, v2.sha)


def test_ref_bump_on_fresh_clone_adopts_locked_output_and_fails_closed_on_edits(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Committed v1 output is adopted and replaced by v2; an edited copy is kept."""
    bumped = _bumped_clone(tmp_path, apm_binary_path)
    clone = bumped.clone
    edited_clone = clone.with_name("edited-clone")
    shutil.copytree(clone, edited_clone)

    result = bumped.runner.run(("install", "--no-policy"), cwd=clone, env=bumped.env)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 0, output
    assert _REFUSAL not in output
    assert bumped.v2_sha in (clone / "apm.lock.yaml").read_text(encoding="utf-8")
    for root in _ROOTS:
        alpha = (clone / root / "alpha/SKILL.md").read_text(encoding="ascii")
        assert "Now use [tools](" in alpha and "](../beta/references/x.md)" not in alpha
    changed = {line[3:] for line in _git(clone, bumped.env, "status", "--porcelain").splitlines()}
    assert changed == {"apm.yml", "apm.lock.yaml", *(f"{r}/alpha/SKILL.md" for r in _ROOTS)}

    edited = edited_clone / ".claude/skills/alpha/SKILL.md"
    edited.write_text("Local edits.\n", encoding="ascii")
    lock_before = (edited_clone / "apm.lock.yaml").read_bytes()
    refused = bumped.runner.run(("install", "--no-policy"), cwd=edited_clone, env=bumped.env)
    refusal = " ".join((refused.stdout + refused.stderr).split())
    assert refused.returncode != 0, refusal
    assert _REFUSAL in refusal
    assert edited.read_text(encoding="ascii") == "Local edits.\n"
    assert (edited_clone / "apm.lock.yaml").read_bytes() == lock_before
