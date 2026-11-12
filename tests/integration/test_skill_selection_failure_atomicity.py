"""Real-binary contract: a failed skills: selection writes no deployed file (#3373)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_MANAGED_ROOTS = (".github", ".agents")


def _evidence(result: CommandResult) -> str:
    """Return stable process evidence when a lifecycle command misbehaves."""
    return (
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _write_skill(package: Path, name: str) -> None:
    """Author one package skill."""
    skill = package / ".apm" / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Demo skill.\n---\n# {name}\n",
        encoding="utf-8",
    )


def _write_instruction(package: Path, skill_name: str) -> None:
    """Author the package instruction that names its companion skill."""
    instructions = package / ".apm" / "instructions"
    instructions.mkdir(parents=True, exist_ok=True)
    (instructions / "review.instructions.md").write_text(
        '---\ndescription: "Demo instruction."\napplyTo: "**"\n---\n\n'
        f"Use the `{skill_name}` skill.\n",
        encoding="utf-8",
    )


def _write_project(project: Path, selected_skill: str) -> None:
    """Write a consumer manifest pinning one skill from the local package."""
    (project / "apm.yml").write_text(
        "name: project\n"
        "version: 1.0.0\n"
        "targets: [copilot]\n"
        "dependencies:\n"
        "  apm:\n"
        "    - path: ../pkg\n"
        f"      skills: [{selected_skill}]\n"
        "  mcp: []\n",
        encoding="utf-8",
    )


def _snapshot(project: Path) -> dict[str, str]:
    """Hash every file under managed target roots plus the lockfile."""
    files: dict[str, str] = {}
    for root_name in _MANAGED_ROOTS:
        root = project / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files[path.relative_to(project).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    lock = project / "apm.lock.yaml"
    files["apm.lock.yaml"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    return files


def test_failed_skill_selection_leaves_deployed_files_byte_identical(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A renamed skill fails the install before the package's instruction is rewritten."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    package = isolated.work_root / "pkg"
    project = isolated.work_root / "project"
    package.mkdir()
    project.mkdir()
    (package / "apm.yml").write_text(
        "name: pkg\nversion: 1.0.0\ntargets: [copilot]\n"
        "includes: [.apm/skills, .apm/instructions]\n",
        encoding="utf-8",
    )
    _write_skill(package, "old-name")
    _write_instruction(package, "old-name")
    _write_project(project, "old-name")
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    def install(scenario_id: str) -> CommandResult:
        return runner.run(
            ("install",),
            scenario_id=scenario_id,
            cwd=project,
            env=isolated.subprocess_env(),
        )

    baseline = install("skill-selection-atomicity-baseline")
    assert baseline.returncode == 0, _evidence(baseline)
    instruction = project / ".github" / "instructions" / "review.instructions.md"
    old_skill = project / ".agents" / "skills" / "old-name" / "SKILL.md"
    assert "`old-name`" in instruction.read_text(encoding="utf-8")
    assert old_skill.is_file()
    before = _snapshot(project)

    # New package revision: the skill is renamed and the instruction follows it.
    (package / ".apm" / "skills" / "old-name" / "SKILL.md").unlink()
    (package / ".apm" / "skills" / "old-name").rmdir()
    _write_skill(package, "new-name")
    _write_instruction(package, "new-name")

    for attempt in ("first", "retry"):
        failed = install(f"skill-selection-atomicity-{attempt}-failure")
        output = " ".join(f"{failed.stdout} {failed.stderr}".split())
        assert failed.returncode == 1, _evidence(failed)
        assert "Skill selection matched no available skills" in output, _evidence(failed)
        assert "No install transaction changes were committed" in output, _evidence(failed)
        assert "instruction(s) integrated" not in output, _evidence(failed)
        assert _snapshot(project) == before, _evidence(failed)

    _write_project(project, "new-name")
    recovered = install("skill-selection-atomicity-recovery")
    assert recovered.returncode == 0, _evidence(recovered)
    assert "`new-name`" in instruction.read_text(encoding="utf-8")
    assert (project / ".agents" / "skills" / "new-name" / "SKILL.md").is_file()
    assert not old_skill.exists()
    lock_text = (project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert ".agents/skills/new-name/SKILL.md" in lock_text
    assert ".agents/skills/old-name" not in lock_text
