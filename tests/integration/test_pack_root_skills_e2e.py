"""Hermetic end-to-end coverage for root-level plugin component packing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run_apm(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the installed APM CLI in a project directory."""
    apm_executable = Path(sys.executable).with_name("apm")
    return subprocess.run(
        [str(apm_executable), *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


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

    result = _run_apm(project, "pack")

    assert result.returncode == 0, (
        f"apm pack failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    bundle = project / "build" / "root-skills-test-1.0.0"
    assert (bundle / "skills" / "published" / "SKILL.md").is_file()
    assert not (bundle / "skills" / "work-in-progress").exists()
    output = " ".join((result.stdout + result.stderr).split())
    assert "Skipping root-level skills/ because .apm/ is present." in output
    assert "Move publishable files to .apm/skills/" in output
    assert "remove skills/ to silence this warning." in output


def test_init_then_pack_preserves_native_claude_skill(tmp_path: Path) -> None:
    """Init must not make a native Claude root skill disappear from pack."""
    project = tmp_path / "native-plugin"
    skill = project / "skills" / "published"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Published\n", encoding="utf-8")

    init_result = _run_apm(project, "init", "--yes")

    assert init_result.returncode == 0, init_result.stderr
    assert not (project / ".apm").exists()
    assert "includes: auto" in (project / "apm.yml").read_text(encoding="utf-8")
    init_output = " ".join((init_result.stdout + init_result.stderr).split())
    assert "Found plugin-native sources at the project root: skills/." in init_output
    assert "They remain included by apm pack." in init_output

    pack_result = _run_apm(project, "pack")

    assert pack_result.returncode == 0, pack_result.stderr
    bundles = [path for path in (project / "build").iterdir() if path.is_dir()]
    assert len(bundles) == 1
    assert (bundles[0] / "skills" / "published" / "SKILL.md").is_file()
