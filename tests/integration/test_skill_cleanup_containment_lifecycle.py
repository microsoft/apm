"""Skill cleanup stays inside the project when the skills root is a link.

``apm uninstall`` without a lockfile falls back to name-based skill cleanup.
A ``.claude/skills`` symlink must never let that cleanup remove directories
outside the project.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._containment_kit import SKILL, build_containment_kit, output, snapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows"),
]


def test_lockfileless_uninstall_keeps_linked_skills_root_contents(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("claude",))
    (kit.project / ".claude").mkdir()
    install = kit.run("install", "--no-policy")
    assert install.returncode == 0, output(install)

    for name in ("kept-one", "kept-two"):
        skill = kit.outside / name / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text(SKILL, encoding="utf-8")
    before = snapshot(kit.outside)
    (kit.project / "apm.lock.yaml").unlink()
    skills_root = kit.project / ".claude/skills"
    skills_root.rename(kit.project / "skills.previous")
    os.symlink(kit.outside, skills_root, target_is_directory=True)

    kit.run("uninstall", kit.package.as_posix())

    assert snapshot(kit.outside) == before
    assert skills_root.is_symlink()
