"""Skill cleanup without lockfile ownership removes nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS


def _skill(root: Path, name: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("body\n", encoding="utf-8")
    return skill


def _sync_without_ownership(project: Path) -> dict[str, int]:
    return SkillIntegrator().sync_integration(
        None, project, managed_files=None, targets=[KNOWN_TARGETS["claude"]]
    )


def test_unowned_skill_directories_are_kept(tmp_path: Path) -> None:
    project = tmp_path / "project"
    kept = _skill(project / ".claude" / "skills", "user-authored")

    assert _sync_without_ownership(project) == {"files_removed": 0, "errors": 0}
    assert (kept / "SKILL.md").is_file()


def test_linked_skills_root_is_left_untouched(tmp_path: Path) -> None:
    project = tmp_path / "project"
    kept = _skill(tmp_path / "outside", "kept")
    skills_dir = project / ".claude" / "skills"
    skills_dir.parent.mkdir(parents=True)
    try:
        skills_dir.symlink_to(kept.parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    assert _sync_without_ownership(project) == {"files_removed": 0, "errors": 0}
    assert (kept / "SKILL.md").is_file()
