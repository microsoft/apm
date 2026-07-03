"""Hermetic e2e coverage for plugin packing filtered skill dependencies."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_e2e_mode,
]


def _write_apm_yml(project: Path) -> None:
    project.joinpath("apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "subset-pack-e2e",
                "version": "1.0.0",
                "description": "Hermetic plugin pack subset fixture",
                "target": "copilot",
                "dependencies": {
                    "apm": [
                        {
                            "git": "acme/skill-bundle",
                            "ref": "abc123",
                            "skills": ["alpha", "beta"],
                        }
                    ],
                    "mcp": [],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_lockfile(project: Path, dep: LockedDependency) -> None:
    lockfile = LockFile()
    lockfile.add_dependency(dep)
    lockfile.write(project / "apm.lock.yaml")


def _write_deployed_skill(project: Path, name: str, marker: str) -> list[str]:
    skill_dir = project / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    notes_file = skill_dir / "notes.md"
    skill_file.write_text(marker, encoding="utf-8")
    notes_file.write_text(f"{marker} notes", encoding="utf-8")
    return [
        skill_dir.relative_to(project).as_posix(),
        skill_file.relative_to(project).as_posix(),
        notes_file.relative_to(project).as_posix(),
    ]


def _write_cached_skill(project: Path, name: str, marker: str) -> None:
    skill_dir = project / "apm_modules" / "acme" / "skill-bundle" / ".apm" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(marker, encoding="utf-8")


def _run_pack(project: Path, output_name: str = "build") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("GITHUB_TOKEN", "GITHUB_APM_PAT", "ADO_APM_PAT"):
        env.pop(name, None)
    home = project / ".home"
    home.mkdir(exist_ok=True)
    env.update(
        {
            "APM_E2E_TESTS": "1",
            "HOME": str(home),
            "PYTHONUTF8": "1",
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "apm_cli.cli",
            "pack",
            "--format",
            "plugin",
            "--output",
            output_name,
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _bundle_dir(project: Path, output_name: str = "build") -> Path:
    return project / output_name / "subset-pack-e2e-1.0.0"


def _assert_only_subset_skills(bundle_dir: Path) -> None:
    skills_dir = bundle_dir / "skills"
    assert {path.name for path in skills_dir.iterdir()} == {"alpha", "beta"}
    assert (skills_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "deployed alpha"
    assert (skills_dir / "beta" / "SKILL.md").read_text(encoding="utf-8") == "deployed beta"
    assert not (skills_dir / "gamma").exists()


def test_pack_plugin_uses_deployed_skill_subset_and_survives_missing_cache(
    tmp_path: Path,
) -> None:
    """apm pack must export the installed subset, not raw apm_modules."""
    project = tmp_path / "project"
    project.mkdir()
    _write_apm_yml(project)
    deployed_files = [
        file
        for skill in ("alpha", "beta")
        for file in _write_deployed_skill(project, skill, f"deployed {skill}")
    ]
    _write_lockfile(
        project,
        LockedDependency(
            repo_url="acme/skill-bundle",
            resolved_commit="abc123",
            depth=1,
            package_type="skill_bundle",
            deployed_files=deployed_files,
            skill_subset=["alpha", "beta"],
        ),
    )
    for skill in ("alpha", "beta", "gamma"):
        _write_cached_skill(project, skill, f"raw cache {skill}")

    result = _run_pack(project)

    assert result.returncode == 0, result.stderr
    _assert_only_subset_skills(_bundle_dir(project))

    shutil.rmtree(project / "apm_modules")
    shutil.rmtree(project / "build")

    result_without_cache = _run_pack(project)

    assert result_without_cache.returncode == 0, result_without_cache.stderr
    _assert_only_subset_skills(_bundle_dir(project))


def test_pack_plugin_rejects_unsafe_deployed_paths(tmp_path: Path) -> None:
    """apm pack must reject unsafe deployed_files paths before copying."""
    project = tmp_path / "project"
    project.mkdir()
    _write_apm_yml(project)
    _write_lockfile(
        project,
        LockedDependency(
            repo_url="acme/skill-bundle",
            resolved_commit="abc123",
            depth=1,
            package_type="skill_bundle",
            deployed_files=[".agents/skills/../escape/SKILL.md"],
            skill_subset=["alpha"],
        ),
    )

    result = _run_pack(project)

    assert result.returncode != 0
    combined_output = result.stdout + result.stderr
    assert "unsafe deployed file path" in combined_output


def test_pack_plugin_older_lockfile_without_deployed_files_uses_cache(
    tmp_path: Path,
) -> None:
    """Older lockfiles still pack dependency skills from apm_modules."""
    project = tmp_path / "project"
    project.mkdir()
    _write_apm_yml(project)
    _write_lockfile(
        project,
        LockedDependency(
            repo_url="acme/skill-bundle",
            resolved_commit="abc123",
            depth=1,
            package_type="skill_bundle",
        ),
    )
    _write_cached_skill(project, "legacy", "legacy cache skill")

    result = _run_pack(project)

    assert result.returncode == 0, result.stderr
    bundle_dir = _bundle_dir(project)
    assert (bundle_dir / "skills" / "legacy" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "legacy cache skill"
