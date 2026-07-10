"""Hermetic end-to-end coverage for root-level plugin component packing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_pack_auto_includes_only_apm_authored_skills(tmp_path: Path) -> None:
    """The real pack command must not treat a root skills directory as publishable."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: root-skills-test\n"
        "version: 1.0.0\n"
        "description: Root skills regression\n"
        "license: MIT\n"
        "target: claude\n"
        "includes: auto\n"
        "dependencies:\n"
        "  apm: []\n",
        encoding="utf-8",
    )

    authored_skill = project / ".apm" / "skills" / "published"
    authored_skill.mkdir(parents=True)
    (authored_skill / "SKILL.md").write_text("# Published\n", encoding="utf-8")

    local_skill = project / "skills" / "work-in-progress"
    local_skill.mkdir(parents=True)
    (local_skill / "SKILL.md").write_text("# Work in progress\n", encoding="utf-8")

    apm_executable = Path(sys.executable).with_name("apm")
    result = subprocess.run(
        [str(apm_executable), "pack"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, (
        f"apm pack failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    bundle = project / "build" / "root-skills-test-1.0.0"
    assert (bundle / "skills" / "published" / "SKILL.md").is_file()
    assert not (bundle / "skills" / "work-in-progress").exists()
