"""Positive source and skill completion receipts never infer success from omissions."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.skill_support import SkillIntegrationResult
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.models.dependency import DependencyReference

pytestmark = pytest.mark.component


def _fixture(tmp_path: Path) -> tuple[PackageInfo, Path, DeployableSourcePlan]:
    source = tmp_path / "source"
    skill = source / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: s\ndescription: Completion\n---\n", encoding="utf-8"
    )
    (skill / "asset.md").write_text("asset\n", encoding="utf-8")
    package = PackageInfo(
        package=APMPackage(name="source", version="1.0.0"),
        install_path=source,
        dependency_ref=DependencyReference.parse(str(source)),
        package_type=PackageType.SKILL_BUNDLE,
    )
    project = tmp_path / "project"
    project.mkdir()
    return package, project, _plan(package)


def _plan(package: PackageInfo) -> DeployableSourcePlan:
    return DeployableSourcePlan.create(
        package,
        [KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]],
        skill_subset=None,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )


def _integrate(
    package: PackageInfo, project: Path, plan: DeployableSourcePlan
) -> SkillIntegrationResult:
    return SkillIntegrator().integrate_package_skill(
        package,
        project,
        targets=[KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]],
        source_plan=plan,
        managed_files=set(),
        skip_bin=True,
    )


def test_completion_is_unknown_without_positive_discovery(tmp_path: Path) -> None:
    package, project, plan = _fixture(tmp_path)
    unknown = DeployableSourcePlan(source_root=plan.source_root, paths=plan.paths)
    assert unknown.skill_discovery_complete is False
    assert _integrate(package, project, unknown).complete is False
    assert SkillIntegrationResult(False, False, True, None, 0).complete is False


def test_completion_covers_both_physical_targets(tmp_path: Path) -> None:
    package, project, plan = _fixture(tmp_path)
    assert plan.skill_discovery_complete is True
    assert plan.skill_source_roots == frozenset({"skills/s"})
    assert plan.skill_source_files == frozenset({"skills/s/SKILL.md", "skills/s/asset.md"})
    result = _integrate(package, project, plan)
    assert result.complete is True
    assert len(result.deployed_files) == 4


def test_default_executable_approval_does_not_expand_skill_completion(tmp_path: Path) -> None:
    package, project, _ = _fixture(tmp_path)
    hooks = package.install_path / ".apm/hooks"
    hooks.mkdir(parents=True)
    (hooks / "hooks.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    plan = DeployableSourcePlan.create(
        package,
        [KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]],
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=True,
        skip_bin=True,
    )
    assert plan.skill_discovery_complete is True
    assert ".apm/hooks/hooks.json" in plan.paths
    assert plan.skill_source_files == frozenset({"skills/s/SKILL.md", "skills/s/asset.md"})
    result = _integrate(package, project, plan)
    assert result.complete is True
    assert {path.name for path in result.deployed_files} == {"SKILL.md", "asset.md"}


@pytest.mark.parametrize("whole_skill", [False, True], ids=["member", "root"])
def test_secondary_target_refusal_cannot_report_complete(tmp_path: Path, whole_skill: bool) -> None:
    package, project, plan = _fixture(tmp_path)
    target = project / ".agents/skills/s"
    target.mkdir(parents=True)
    (target / "asset.md").write_text("personal\n", encoding="utf-8")
    integrator = SkillIntegrator()
    managed = set() if whole_skill else {".agents/skills/s"}
    result = integrator.integrate_package_skill(
        package,
        project,
        targets=[KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]],
        source_plan=plan,
        managed_files=managed,
        skip_bin=True,
    )
    assert result.complete is False
    assert (target / "asset.md").read_text(encoding="utf-8") == "personal\n"
    assert (project / ".claude/skills/s/SKILL.md").is_file()


def test_admitted_member_disappearing_after_discovery_is_incomplete(tmp_path: Path) -> None:
    package, project, plan = _fixture(tmp_path)
    (package.install_path / "skills/s/asset.md").unlink()
    assert _integrate(package, project, plan).complete is False


def test_missing_admitted_skill_root_is_incomplete(tmp_path: Path) -> None:
    package, project, plan = _fixture(tmp_path)
    (package.install_path / "skills/s").rename(package.install_path / "skills/moved")
    assert _integrate(package, project, plan).complete is False


def test_identical_members_are_successful_materializations(tmp_path: Path) -> None:
    package, project, plan = _fixture(tmp_path)
    assert _integrate(package, project, plan).complete is True
    assert _integrate(package, project, plan).complete is True


@pytest.mark.parametrize("baseline", ["valid", "missing", "changed"])
def test_required_identical_write_keeps_normal_hash_guard_and_actual_copy_callback(
    tmp_path: Path, baseline: str
) -> None:
    from apm_cli.utils.content_hash import compute_file_hash

    package, project, _ = _fixture(tmp_path)
    source = package.install_path / "skills/s"
    destination = project / ".agents/skills/s"
    shutil.copytree(source, destination)
    written = []
    with patch("apm_cli.integration.base_integrator.shutil.copy2", wraps=shutil.copy2) as copy:
        copied = BaseIntegrator().copy_directory_files(
            source,
            destination,
            ignore=lambda _directory, _names: [],
            path_entry=lambda path: path.relative_to(project).as_posix(),
            is_managed=lambda _entry: True,
            recorded_hashes={},
            baseline_hash=lambda path: (
                compute_file_hash(path)
                if baseline == "valid"
                else None
                if baseline == "missing"
                else "sha256:" + "0" * 64
            ),
            on_member_written=written.append,
            requires_write=lambda _path: True,
            unchanged=True,
        )
    assert copy.call_count == (2 if baseline == "valid" else 0)
    assert written == copied
    assert len(written) == copy.call_count
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == {
        path.name: path.read_bytes() for path in source.iterdir()
    }


def test_replay_is_not_source_completion_authority(tmp_path: Path) -> None:
    from apm_cli.deps.lockfile import LockFile

    package, project, plan = _fixture(tmp_path)
    integrator = SkillIntegrator()
    with integrator.ownership_snapshot(replay_lockfile=LockFile()):
        result = integrator.integrate_package_skill(
            package,
            project,
            targets=[KNOWN_TARGETS["claude"]],
            source_plan=plan,
            managed_files=set(),
        )
    assert result.deployed_files
    assert result.complete is False


@pytest.mark.parametrize("stage", ["discovery", "copy"])
def test_walk_errors_fail_explicitly_instead_of_completing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    package, project, plan = _fixture(tmp_path)
    scandir = os.scandir

    def fail_skill_walk(path: Path | str) -> Iterator[os.DirEntry[str]]:
        if Path(path) == package.install_path / "skills/s":
            raise PermissionError("injected source enumeration refusal")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", fail_skill_walk)
    with pytest.raises(OSError, match="enumerat"):
        if stage == "discovery":
            _plan(package)
        else:
            _integrate(package, project, plan)


@pytest.mark.parametrize("stage", ["copy", "rewrite", "hash"])
def test_post_discovery_failures_never_publish_success(tmp_path: Path, stage: str) -> None:
    package, project, plan = _fixture(tmp_path)
    integrator = SkillIntegrator()
    target = {
        "copy": "apm_cli.integration.base_integrator.shutil.copy2",
        "rewrite": "apm_cli.integration.skill_integrator.SkillIntegrator._resolve_markdown_links_in_skill_bundle",
        "hash": "apm_cli.utils.content_hash.compute_file_hash",
    }[stage]
    with patch(target, side_effect=OSError("injected materialization refusal")):
        with pytest.raises(OSError, match="injected materialization refusal"):
            integrator.integrate_package_skill(
                package,
                project,
                targets=[KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]],
                source_plan=plan,
                managed_files=set(),
                skip_bin=True,
            )
    assert integrator._skill_files == []
    assert integrator._skill_session_hashes == {}
    assert integrator._skill_completed_roots == {}
    assert integrator._skill_completed_files == {}


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_copy_failure_only_rolls_back_new_empty_destination(
    tmp_path: Path, existing: bool, partial: bool
) -> None:
    package, project, plan = _fixture(tmp_path)
    target = project / ".claude/skills/s"
    if existing:
        target.mkdir(parents=True)
    integrator = SkillIntegrator()

    def fail_copy(source: Path, destination: Path) -> None:
        if partial:
            destination.write_bytes(b"partial write")
        raise OSError("injected first-copy failure")

    with patch("apm_cli.integration.base_integrator.shutil.copy2", side_effect=fail_copy):
        with pytest.raises(OSError, match="injected first-copy failure"):
            integrator.integrate_package_skill(
                package,
                project,
                targets=[KNOWN_TARGETS["claude"]],
                source_plan=plan,
                managed_files={".claude/skills/s"} if existing else set(),
            )
    assert target.exists() is (existing or partial)
    assert not integrator._skill_files
    assert not integrator._skill_session_hashes
    assert not integrator._native_skill_session_owners
    if partial:
        assert (target / "SKILL.md").read_bytes() == b"partial write"
    result = integrator.integrate_package_skill(
        package,
        project,
        targets=[KNOWN_TARGETS["claude"]],
        source_plan=plan,
        managed_files=set(),
    )
    assert result.complete is (not existing and not partial)
    if result.complete:
        assert (target / "SKILL.md").read_bytes() == (
            package.install_path / "skills/s/SKILL.md"
        ).read_bytes()


@pytest.mark.parametrize("removed", ["source", "target"])
def test_member_disappearing_before_rewrite_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, removed: str
) -> None:
    package, project, plan = _fixture(tmp_path)
    copy_file = shutil.copy2

    def copy_then_remove(source: Path, target: Path) -> Path:
        copied = copy_file(source, target)
        if source.name == "asset.md":
            (source if removed == "source" else target).unlink()
        return Path(copied)

    monkeypatch.setattr(shutil, "copy2", copy_then_remove)
    with pytest.raises(OSError, match="Cannot finalize copied skill member"):
        _integrate(package, project, plan)
    assert (project / ".claude/skills/s/SKILL.md").is_file()


def test_unknown_link_initialization_does_not_supply_completion(tmp_path: Path) -> None:
    package, project, plan = _fixture(tmp_path)
    with patch.object(SkillIntegrator, "init_link_resolver", return_value=None):
        result = _integrate(package, project, plan)
    assert len(result.deployed_files) == 4
    assert result.complete is False


@pytest.mark.parametrize("reason", ["collision", "edited", "replay"])
def test_member_skip_signal_does_not_depend_on_diagnostics(tmp_path: Path, reason: str) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "a.md").write_text("source", encoding="utf-8")
    (target / "a.md").write_text("personal", encoding="utf-8")
    skipped: list[Path] = []
    copied = BaseIntegrator().copy_directory_files(
        source,
        target,
        ignore=lambda directory, names: [],
        path_entry=lambda path: path.name,
        is_managed=lambda path: reason != "collision",
        recorded_hashes={"a.md": "sha256:previous"} if reason == "edited" else {},
        write_allowed=lambda path: reason != "replay",
        on_member_skipped=skipped.append,
    )
    assert copied == []
    assert skipped == [target / "a.md"]
    assert (target / "a.md").read_text(encoding="utf-8") == "personal"


def test_missing_copy_root_is_an_explicit_failure(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="enumerat"):
        list(BaseIntegrator.walk_source_tree(tmp_path / "missing"))


def test_optional_absent_sources_have_positive_empty_discovery(tmp_path: Path) -> None:
    plan = DeployableSourcePlan.create(
        SimpleNamespace(install_path=tmp_path),
        [KNOWN_TARGETS["claude"]],
        skill_subset=None,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )
    assert plan.skill_discovery_complete is True
    assert not plan.skill_source_roots
    assert not plan.skill_source_files


def test_non_directory_source_root_cannot_authorize_empty_discovery(tmp_path: Path) -> None:
    package, _, _ = _fixture(tmp_path)
    (package.install_path / "skills").rename(package.install_path / "old-skills")
    (package.install_path / "skills").write_text("not a source tree\n", encoding="utf-8")
    assert _plan(package).skill_discovery_complete is False


def test_symlink_skill_root_cannot_authorize_empty_discovery(tmp_path: Path) -> None:
    package, _, _ = _fixture(tmp_path)
    skills = package.install_path / "skills"
    outside = tmp_path / "outside-skills"
    skills.rename(outside)
    skills.symlink_to(outside, target_is_directory=True)
    plan = _plan(package)
    assert plan.skill_discovery_complete is False
    assert plan.skill_source_roots == frozenset()
    assert plan.skill_source_files == frozenset()


def test_unreadable_skill_root_is_not_an_absent_optional_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _, _ = _fixture(tmp_path)
    original_stat = Path.stat

    def denied_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == package.install_path / "skills" and follow_symlinks:
            raise PermissionError("injected source stat denial")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", denied_stat)
    with pytest.raises(PermissionError, match="injected source stat denial"):
        _plan(package)


def test_member_disappearing_during_discovery_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _, _ = _fixture(tmp_path)
    member = package.install_path / "skills/s/asset.md"
    original_is_file = Path.is_file

    def disappearing_is_file(path: Path) -> bool:
        if path == member and path.exists():
            path.unlink()
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", disappearing_is_file)
    assert _plan(package).skill_discovery_complete is False


def test_partial_selection_does_not_certify_complete_discovery(tmp_path: Path) -> None:
    package, project, _ = _fixture(tmp_path)
    plan = DeployableSourcePlan.create(
        package,
        [KNOWN_TARGETS["claude"]],
        skill_subset=("s", "missing"),
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )
    assert plan.skill_discovery_complete is False
    assert _integrate(package, project, plan).complete is False


def test_shared_physical_target_dedup_keeps_completion(tmp_path: Path) -> None:
    package, project, plan = _fixture(tmp_path)
    result = SkillIntegrator().integrate_package_skill(
        package,
        project,
        targets=[KNOWN_TARGETS["copilot"], KNOWN_TARGETS["codex"]],
        source_plan=plan,
        managed_files=set(),
        skip_bin=True,
    )
    assert result.complete is True
    assert len(result.deployed_files) == 2


def test_no_skill_target_does_not_certify_completion(tmp_path: Path) -> None:
    package, project, plan = _fixture(tmp_path)
    result = SkillIntegrator().integrate_package_skill(
        package,
        project,
        targets=[],
        source_plan=plan,
        managed_files=set(),
    )
    assert not result.deployed_files
    assert result.complete is False
