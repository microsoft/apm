"""Real-CLI lifecycle regression proof for nested git worktree cleanup."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.compilation.distributed_compiler import AGENTS_MD_GENERATED_MARKER
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_GIT_TIMEOUT_SECONDS = 30
_GENERATED_AGENTS = f"{AGENTS_MD_GENERATED_MARKER}\n# Nested worktree instructions\n".encode()


def _run_git(
    cwd: Path, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    """Run git in a lifecycle workspace with captured diagnostics."""
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _git_environment(base_env: dict[str, str]) -> dict[str, str]:
    """Provide deterministic identity for commits in the nested worktree."""
    return {
        **base_env,
        "GIT_AUTHOR_NAME": "APM Test",
        "GIT_AUTHOR_EMAIL": "apm-test@example.invalid",
        "GIT_COMMITTER_NAME": "APM Test",
        "GIT_COMMITTER_EMAIL": "apm-test@example.invalid",
    }


def test_compile_clean_preserves_nested_git_worktree_agents_file(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """``compile --clean`` removes parent orphans without touching a nested worktree."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "workspace", base_env=dict(os.environ))
    environment = _git_environment(isolated.subprocess_env())
    parent = isolated.work_root / "parent"
    parent.mkdir()
    _run_git(parent, environment, "init", "--initial-branch=main")
    (parent / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (parent / "apm.yml").write_text("name: nested-worktree\nversion: 1.0.0\n", encoding="utf-8")
    instructions = parent / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "root.instructions.md").write_text(
        "---\ndescription: Root lifecycle fixture\napplyTo: '**/*.py'\n---\nUse focused tests.\n",
        encoding="utf-8",
    )
    _run_git(parent, environment, "add", "--all")
    _run_git(parent, environment, "commit", "-m", "seed parent")

    nested = parent / ".worktrees" / "nested"
    _run_git(parent, environment, "worktree", "add", "-b", "nested", str(nested))
    nested_agents = nested / "AGENTS.md"
    nested_agents.write_bytes(_GENERATED_AGENTS)
    nested_descendant_agents = nested / "subdir" / "AGENTS.md"
    nested_descendant_agents.parent.mkdir()
    nested_descendant_agents.write_bytes(_GENERATED_AGENTS)
    _run_git(nested, environment, "add", "AGENTS.md", "subdir/AGENTS.md")
    _run_git(nested, environment, "commit", "-m", "seed nested agents")
    nested_agents_before = nested_agents.read_bytes()
    nested_descendant_agents_before = nested_descendant_agents.read_bytes()

    parent_orphan = parent / "stale" / "AGENTS.md"
    parent_orphan.parent.mkdir()
    parent_orphan.write_bytes(_GENERATED_AGENTS)

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("compile", "--clean"),
        scenario_id="compile-clean-nested-worktree",
        cwd=parent,
        env=environment,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert not parent_orphan.exists()
    assert nested_agents.read_bytes() == nested_agents_before
    assert nested_descendant_agents.read_bytes() == nested_descendant_agents_before
    assert _run_git(nested, environment, "status", "--porcelain").stdout == ""
