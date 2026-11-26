"""The sibling-skill link plan mirrors skill deployment for one target (#5523)."""

from pathlib import Path

import pytest

from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.integration.skill_preflight import planned_skill_files
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.models.dependency import DependencyReference

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-skills-root-derivation")]


def _package(tmp_path: Path) -> PackageInfo:
    root = tmp_path / "package" / "review"
    source = root / ".apm" / "skills" / "review"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_bytes(
        b"---\nname: review\ndescription: Review\n---\nPackage review.\n"
    )
    return PackageInfo(
        package=APMPackage(name="review", version="1.0.0", package_path=root),
        install_path=root,
        dependency_ref=DependencyReference.parse("../review"),
        package_type=PackageType.APM_PACKAGE,
    )


def _plan(info: PackageInfo, subset: tuple[str, ...] | None = None) -> DeployableSourcePlan:
    return DeployableSourcePlan.create(
        info,
        [KNOWN_TARGETS["copilot"]],
        skill_subset=subset,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )


def test_planned_skill_files_maps_selected_skill_files_to_target_destination(tmp_path: Path):
    """Agents link to the deployed copy of a sibling skill (#5523)."""
    info = _package(tmp_path)
    source = info.install_path.resolve() / ".apm" / "skills" / "review"
    (source / "refs").mkdir()
    (source / "refs" / "guide.md").write_text("guide", encoding="utf-8")
    project = tmp_path / "project"
    target = KNOWN_TARGETS["copilot"]

    planned = planned_skill_files(info, target, project, _plan(info))

    destination = project / target.skills_rel_root / "review"
    assert planned == {
        source / "SKILL.md": destination / "SKILL.md",
        source / "refs" / "guide.md": destination / "refs" / "guide.md",
    }
    assert all(path.is_file() for path in planned)


def test_planned_skill_files_is_empty_for_unselected_skill_or_unknown_plan(tmp_path: Path):
    info = _package(tmp_path)
    (info.install_path / ".apm" / "skills" / "other").mkdir()
    (info.install_path / ".apm" / "skills" / "other" / "SKILL.md").write_text(
        "---\nname: other\ndescription: d\n---\n", encoding="utf-8"
    )
    target = KNOWN_TARGETS["copilot"]
    project = tmp_path / "project"

    planned = planned_skill_files(info, target, project, _plan(info, ("other",)))

    assert {path.parent.name for path in planned} == {"other"}
    assert all(path.is_file() for path in planned)
    assert planned_skill_files(info, target, project, None) == {}


def test_planned_skill_files_honours_the_effective_pairing_for_the_target(tmp_path: Path):
    """The caller's effective pairing, not the raw dependency one, narrows the plan."""
    from apm_cli.models.dependency.subsets import SkillTargetSelection, SubsetPairing

    info = _package(tmp_path)
    project = tmp_path / "project"
    pairing = SkillTargetSelection((SubsetPairing(("review",), ("claude",)),))

    plan = _plan(info)
    assert planned_skill_files(info, KNOWN_TARGETS["copilot"], project, plan, pairing) == {}
    claude = planned_skill_files(info, KNOWN_TARGETS["claude"], project, plan, pairing)
    assert claude
    assert all(path.is_file() for path in claude)
