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
_MANAGED_START = "<!-- apm:start -->"
_MANAGED_END = "<!-- apm:end -->"


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


def test_compile_excludes_linked_worktree_primitives(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Compile only primitives owned by the active Git checkout."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "workspace", base_env=dict(os.environ))
    environment = _git_environment(isolated.subprocess_env())
    parent = isolated.work_root / "parent"
    parent.mkdir()
    _run_git(parent, environment, "init", "--initial-branch=main")
    (parent / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (parent / "apm.yml").write_text(
        "name: nested-worktree-discovery\n"
        "version: 1.0.0\n"
        "target:\n"
        "  - claude\n"
        "  - codex\n"
        "includes: auto\n",
        encoding="utf-8",
    )
    instructions = parent / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "own.instructions.md").write_text(
        "---\ndescription: Main only.\n---\n# Own\nOWN-SENTINEL\n",
        encoding="utf-8",
    )
    _run_git(parent, environment, "add", "--all")
    _run_git(parent, environment, "commit", "-m", "seed parent")
    _run_git(parent, environment, "checkout", "-b", "other")
    (instructions / "foreign.instructions.md").write_text(
        "---\ndescription: Other branch only.\n---\n# Foreign\nFOREIGN-SENTINEL\n",
        encoding="utf-8",
    )
    _run_git(parent, environment, "add", "--all")
    _run_git(parent, environment, "commit", "-m", "seed other branch")
    _run_git(parent, environment, "checkout", "main")
    nested = parent / ".worktrees" / "other"
    _run_git(parent, environment, "worktree", "add", str(nested), "other")

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("compile",),
        scenario_id="compile-excludes-linked-worktree-primitives",
        cwd=parent,
        env=environment,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    for output_name in ("AGENTS.md", "CLAUDE.md"):
        output = (parent / output_name).read_text(encoding="utf-8")
        assert "OWN-SENTINEL" in output
        assert "FOREIGN-SENTINEL" not in output
    assert "foreign.instructions.md" not in result.stdout
    assert "foreign.instructions.md" not in result.stderr


def test_compile_root_reports_skipped_nested_repository_placement(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Root redirection reports outputs omitted at the destination boundary."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "workspace", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    source = isolated.work_root / "source"
    source.mkdir()
    (source / "apm.yml").write_text(
        "name: nested-root-output\nversion: 1.0.0\ntarget: codex\n",
        encoding="utf-8",
    )
    instructions = source / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "root.instructions.md").write_text(
        "---\ndescription: Root.\n---\nROOT-SENTINEL\n",
        encoding="utf-8",
    )
    (instructions / "scoped.instructions.md").write_text(
        "---\ndescription: Scoped.\napplyTo: 'src/**/*.py'\n---\nSCOPED-SENTINEL\n",
        encoding="utf-8",
    )
    (source / "src").mkdir()
    (source / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    destination = isolated.work_root / "destination"
    (destination / "src" / ".git").mkdir(parents=True)

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("compile", "--root", str(destination), "--force-instructions"),
        scenario_id="compile-reports-skipped-nested-placement",
        cwd=source,
        env=environment,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert (destination / "AGENTS.md").is_file()
    assert not (destination / "src" / "AGENTS.md").exists()
    assert "Compiled 1 output file (1 AGENTS.md file)" in result.stdout
    unwrapped_stdout = result.stdout.replace("\n", "")
    assert (
        f"Run apm compile from {destination / 'src'} to compile it separately" in unwrapped_stdout
    )


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
    (parent / "apm.yml").write_text(
        "name: nested-worktree\nversion: 1.0.0\ncompilation:\n  exclude:\n    - excluded\n",
        encoding="utf-8",
    )
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

    nested_repository = parent / "nested-repository"
    nested_repository.mkdir()
    _run_git(nested_repository, environment, "init", "--initial-branch=main")
    nested_repository_agents = nested_repository / "AGENTS.md"
    nested_repository_agents.write_bytes(_GENERATED_AGENTS)
    _run_git(nested_repository, environment, "add", "AGENTS.md")
    _run_git(nested_repository, environment, "commit", "-m", "seed nested repository agents")
    nested_repository_agents_before = nested_repository_agents.read_bytes()

    parent_orphan = parent / "stale" / "AGENTS.md"
    parent_orphan.parent.mkdir()
    parent_orphan.write_bytes(_GENERATED_AGENTS)
    excluded_orphan = parent / "excluded" / "AGENTS.md"
    excluded_orphan.parent.mkdir()
    excluded_orphan.write_bytes(_GENERATED_AGENTS)
    hand_authored = parent / "hand-authored" / "AGENTS.md"
    hand_authored.parent.mkdir()
    hand_authored.write_bytes(b"# Team-owned instructions\n")
    hand_authored_before = hand_authored.read_bytes()

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("compile", "--clean"),
        scenario_id="compile-clean-nested-worktree",
        cwd=parent,
        env=environment,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert not parent_orphan.exists()
    assert not excluded_orphan.exists()
    assert hand_authored.read_bytes() == hand_authored_before
    assert nested_agents.read_bytes() == nested_agents_before
    assert nested_descendant_agents.read_bytes() == nested_descendant_agents_before
    assert nested_repository_agents.read_bytes() == nested_repository_agents_before
    assert _run_git(nested, environment, "status", "--porcelain").stdout == ""
    assert _run_git(nested_repository, environment, "status", "--porcelain").stdout == ""


def test_compile_force_instructions_preserves_managed_files_and_nested_repositories(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Forced Copilot compilation updates only eligible managed AGENTS.md files."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "workspace", base_env=dict(os.environ))
    environment = _git_environment(isolated.subprocess_env())
    parent = isolated.work_root / "parent"
    parent.mkdir()
    _run_git(parent, environment, "init", "--initial-branch=main")
    (parent / ".gitignore").write_text(
        "nested-gitdir/\nnested-gitfile/\n",
        encoding="utf-8",
    )
    (parent / "apm.yml").write_text(
        "name: nested-worktree\nversion: 1.0.0\n"
        "compilation:\n  agents_md:\n    mode: managed_section\n",
        encoding="utf-8",
    )
    instructions = parent / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "root.instructions.md").write_text(
        "---\ndescription: Root lifecycle fixture\napplyTo: '**/*.py'\n---\nUse focused tests.\n",
        encoding="utf-8",
    )
    (instructions / "src.instructions.md").write_text(
        "---\ndescription: Source lifecycle fixture\napplyTo: 'src/**/*.py'\n---\nKeep source changes local.\n",
        encoding="utf-8",
    )
    (instructions / "new.instructions.md").write_text(
        "---\ndescription: New lifecycle fixture\napplyTo: 'new/**/*.py'\n---\nGenerate new guidance.\n",
        encoding="utf-8",
    )
    (parent / "src").mkdir()
    (parent / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    (parent / "new").mkdir()
    (parent / "new" / "module.py").write_text("value = 2\n", encoding="utf-8")
    _run_git(parent, environment, "add", "--all")
    _run_git(parent, environment, "commit", "-m", "seed parent")

    nested_gitdir = parent / "nested-gitdir"
    nested_gitdir.mkdir()
    _run_git(nested_gitdir, environment, "init", "--initial-branch=main")
    nested_gitdir_agents = nested_gitdir / "AGENTS.md"
    nested_gitdir_agents.write_bytes(b"# Nested gitdir guidance\r\n")
    nested_gitdir_descendant = nested_gitdir / "src" / "AGENTS.md"
    nested_gitdir_descendant.parent.mkdir()
    nested_gitdir_descendant.write_bytes(b"# Nested gitdir child guidance\r\n")
    _run_git(nested_gitdir, environment, "add", "--all")
    _run_git(nested_gitdir, environment, "commit", "-m", "seed nested gitdir")

    nested_gitfile = parent / "nested-gitfile"
    _run_git(parent, environment, "worktree", "add", "-b", "nested-gitfile", str(nested_gitfile))
    nested_gitfile_agents = nested_gitfile / "AGENTS.md"
    nested_gitfile_agents.write_bytes(b"# Nested gitfile guidance\r\n")
    nested_gitfile_descendant = nested_gitfile / "src" / "AGENTS.md"
    nested_gitfile_descendant.parent.mkdir(exist_ok=True)
    nested_gitfile_descendant.write_bytes(b"# Nested gitfile child guidance\r\n")
    _run_git(nested_gitfile, environment, "add", "--all")
    _run_git(nested_gitfile, environment, "commit", "-m", "seed nested gitfile")

    root_agents = parent / "AGENTS.md"
    root_agents.write_bytes(
        f"# Root guidance\r\n{_MANAGED_START}\r\nOld generated root\r\n"
        f"{_MANAGED_END}\r\nRoot footer\r\n".encode()
    )
    src_agents = parent / "src" / "AGENTS.md"
    src_agents.write_bytes(
        f"# Source guidance\r\n{_MANAGED_START}\r\nOld generated source\r\n"
        f"{_MANAGED_END}\r\nSource footer\r\n".encode()
    )
    root_before = root_agents.read_bytes()
    src_before = src_agents.read_bytes()
    nested_snapshots = {
        path: path.read_bytes()
        for path in (
            nested_gitdir_agents,
            nested_gitdir_descendant,
            nested_gitfile_agents,
            nested_gitfile_descendant,
        )
    }

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("compile", "--target", "copilot", "--force-instructions"),
        scenario_id="distributed-agents-owned-boundaries",
        cwd=parent,
        env=environment,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    for before, after, old_block in (
        (root_before, root_agents.read_bytes(), b"Old generated root"),
        (src_before, src_agents.read_bytes(), b"Old generated source"),
    ):
        prefix, _, suffix = before.partition(_MANAGED_START.encode())
        _, end_marker, suffix = suffix.partition(_MANAGED_END.encode())
        assert after.startswith(prefix + _MANAGED_START.encode())
        assert after.endswith(end_marker + suffix)
        assert old_block not in after
        assert AGENTS_MD_GENERATED_MARKER.encode() in after
    new_agents = parent / "new" / "AGENTS.md"
    assert AGENTS_MD_GENERATED_MARKER.encode() in new_agents.read_bytes()
    for path, snapshot in nested_snapshots.items():
        assert path.read_bytes() == snapshot
        assert AGENTS_MD_GENERATED_MARKER.encode() not in snapshot
    assert _run_git(nested_gitdir, environment, "status", "--porcelain").stdout == ""
    assert _run_git(nested_gitfile, environment, "status", "--porcelain").stdout == ""

    managed_paths = (root_agents, src_agents, new_agents)
    first_run_bytes = {path: path.read_bytes() for path in managed_paths}
    repeat = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("compile", "--target", "copilot", "--force-instructions"),
        scenario_id="distributed-agents-owned-boundaries-idempotence",
        cwd=parent,
        env=environment,
    )
    assert repeat.returncode == 0, f"stdout={repeat.stdout!r}\nstderr={repeat.stderr!r}"
    assert {path: path.read_bytes() for path in managed_paths} == first_run_bytes
    assert _run_git(nested_gitdir, environment, "status", "--porcelain").stdout == ""
    assert _run_git(nested_gitfile, environment, "status", "--porcelain").stdout == ""

    managed_orphan = parent / "stale" / "AGENTS.md"
    managed_orphan.parent.mkdir()
    managed_orphan.write_bytes(
        f"{AGENTS_MD_GENERATED_MARKER}\n# Retained guidance\n{_MANAGED_START}\n"
        f"Old generated content\n{_MANAGED_END}\n".encode()
    )
    managed_orphan_before = managed_orphan.read_bytes()
    preview = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("compile", "--target", "copilot", "--force-instructions", "--dry-run", "--clean"),
        scenario_id="distributed-agents-managed-orphan-preview",
        cwd=parent,
        env=environment,
    )
    assert preview.returncode == 0, f"stdout={preview.stdout!r}\nstderr={preview.stderr!r}"
    assert managed_orphan.read_bytes() == managed_orphan_before
    assert "Retained managed AGENTS.md orphan: stale/AGENTS.md" in preview.stdout
    assert "remove it manually" in preview.stdout
    assert _run_git(nested_gitdir, environment, "status", "--porcelain").stdout == ""
    assert _run_git(nested_gitfile, environment, "status", "--porcelain").stdout == ""

    src_agents.write_bytes(b"# Missing managed markers\n")
    generated_orphan = parent / "ordinary-stale" / "AGENTS.md"
    generated_orphan.parent.mkdir()
    generated_orphan.write_bytes(_GENERATED_AGENTS)
    generated_orphan_before = generated_orphan.read_bytes()
    invalid_run = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("compile", "--target", "copilot", "--force-instructions", "--clean"),
        scenario_id="distributed-agents-managed-section-preflight",
        cwd=parent,
        env=environment,
    )
    assert invalid_run.returncode != 0
    assert root_agents.read_bytes() == first_run_bytes[root_agents]
    assert new_agents.read_bytes() == first_run_bytes[new_agents]
    assert managed_orphan.read_bytes() == managed_orphan_before
    assert generated_orphan.read_bytes() == generated_orphan_before
