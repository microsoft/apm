"""Root native skills must honor ordinary BaseIntegrator collision protection."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS, apply_legacy_skill_paths
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component


def package(root: Path) -> PackageInfo:
    """Model the ordinary staged local dependency, not an onboarding install."""
    staged = root / "apm_modules/_local/review"
    staged.mkdir(parents=True)
    (staged / "SKILL.md").write_bytes(b"---\nname: review\n---\nPackage skill\n")
    (staged / "payload.bin").write_bytes(b"\x00\xffpackage")
    return PackageInfo(
        package=APMPackage(name="review", version="1.0.0", package_path=staged),
        install_path=staged,
        dependency_ref=DependencyReference.parse("./.claude/skills/review"),
        package_type=PackageType.CLAUDE_SKILL,
    )


def snapshot(path: Path) -> dict[str, bytes]:
    """Capture the entire skill directory, including non-markdown payloads."""
    return {
        str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()
    }


@pytest.mark.parametrize("managed", [None, set()])
def test_native_skill_preserves_unowned_directory(tmp_path: Path, managed: set[str] | None) -> None:
    info = package(tmp_path)
    target = tmp_path / ".agents/skills/review"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"Author's CRLF skill\r\n")
    (target / "custom.bin").write_bytes(b"\xff\x00author")
    original = snapshot(target)
    integrator = SkillIntegrator()
    for _ in range(2):
        result = integrator.integrate_package_skill(
            info,
            tmp_path,
            targets=[KNOWN_TARGETS["copilot"]],
            managed_files=managed,
            diagnostics=DiagnosticCollector(),
        )
        assert snapshot(target) == original
        assert result.target_paths == []
        assert result.skill_skipped
        assert not result.skill_created
        assert not result.skill_updated


def test_native_skill_force_still_replaces_unowned_directory(tmp_path: Path) -> None:
    info = package(tmp_path)
    target = tmp_path / ".agents/skills/review"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"Author skill")
    result = SkillIntegrator().integrate_package_skill(
        info, tmp_path, targets=[KNOWN_TARGETS["copilot"]], managed_files=set(), force=True
    )
    assert result.target_paths == [target]
    assert snapshot(target) == snapshot(info.install_path)


def test_native_skill_ownership_does_not_cross_target_paths(tmp_path: Path) -> None:
    info = package(tmp_path)
    targets = apply_legacy_skill_paths([KNOWN_TARGETS["copilot"], KNOWN_TARGETS["claude"]])
    second = SkillIntegrator._target_skill_dir(targets[1], tmp_path, "review")
    second.mkdir(parents=True)
    (second / "SKILL.md").write_bytes(b"Second target belongs to its author")
    original = snapshot(second)
    result = SkillIntegrator().integrate_package_skill(
        info, tmp_path, targets=targets, managed_files=set(), diagnostics=DiagnosticCollector()
    )
    assert snapshot(second) == original
    assert second not in result.target_paths
    assert len(result.target_paths) == 1
