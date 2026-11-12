"""Legacy skill cleanup only removes regular skill directories inside the project."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.skill_support import clean_orphaned_skills


def _skill(root: Path, name: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("body\n", encoding="utf-8")
    return skill


def _clean(skills_dir: Path, project: Path) -> dict[str, int]:
    return clean_orphaned_skills(
        skills_dir,
        set(),
        project_root=project,
        get_lockfile_owned_agent_skills=lambda _root: set(),
    )


def test_linked_skills_root_is_left_untouched(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    kept = _skill(outside, "kept")
    skills_dir = project / ".claude" / "skills"
    skills_dir.parent.mkdir(parents=True)
    try:
        skills_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    assert _clean(skills_dir, project) == {"files_removed": 0, "errors": 0}
    assert (kept / "SKILL.md").is_file()


def test_linked_skill_entry_is_left_untouched(tmp_path: Path) -> None:
    project = tmp_path / "project"
    kept = _skill(tmp_path / "outside", "kept")
    skills_dir = project / ".claude" / "skills"
    removed = _skill(skills_dir, "orphan")
    try:
        (skills_dir / "linked").symlink_to(kept, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    assert _clean(skills_dir, project) == {"files_removed": 1, "errors": 0}
    assert not removed.exists()
    assert (skills_dir / "linked").is_symlink()
    assert (kept / "SKILL.md").is_file()
