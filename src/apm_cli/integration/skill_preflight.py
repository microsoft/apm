"""Read-only skill source routing and unmanaged-file collision preflight."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from apm_cli.install.deployed_paths import deployed_path_entry
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.utils.diagnostics import DiagnosticCollector, printable_ascii_text

if TYPE_CHECKING:
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.integration.skill_integrator import SkillIntegrator
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.models.apm_package import PackageInfo


@dataclass(frozen=True)
class SkillSourceSelection:
    """The existing native, bundle, or subskill route, before subset filtering."""

    route: Literal["native", "bundle", "subskills", "none"]
    native_root: Path | None
    children: dict[str, Path]


def selected_skill_sources(package_info: PackageInfo) -> SkillSourceSelection:
    """Resolve the one source route consumed by deployment and preflight."""
    from apm_cli.integration.skill_integrator import SkillIntegrator, should_install_skill
    from apm_cli.models.apm_package import PackageType

    root = package_info.install_path.resolve()
    subskills = root / ".apm" / "skills"

    def local_children() -> dict[str, Path]:
        return {
            name: subskills / name for name in SkillIntegrator._skill_names_in_directory(subskills)
        }

    if not should_install_skill(package_info):
        return SkillSourceSelection("subskills", None, local_children())
    dependency = package_info.dependency_ref
    if dependency and dependency.is_virtual and not dependency.is_virtual_subdirectory():
        return SkillSourceSelection("none", None, {})
    if (root / "SKILL.md").exists():
        return SkillSourceSelection("native", root, local_children())
    children = SkillIntegrator.skill_source_paths(root, package_info.package_type)
    if package_info.package_type is PackageType.MARKETPLACE_PLUGIN or (
        children and all(source.parent == root / "skills" for source in children.values())
    ):
        return SkillSourceSelection("bundle", None, children)
    return SkillSourceSelection("subskills", None, local_children())


@dataclass(frozen=True)
class SkillConflict:
    """A differing unowned file at a selected physical skill destination."""

    skill: str
    package: str
    source: Path
    path: str
    targets: tuple[str, ...]

    def message(self) -> str:
        """Name the source, local file and affected targets in ordinary output."""
        return printable_ascii_text(
            f"Installation refused: skill '{self.skill}' from '{self.package}' "
            f"(source: {self.source}) conflicts with unmanaged file '{self.path}'. "
            f"Affected targets: {', '.join(self.targets)}."
        )


def format_skill_conflicts(conflicts: Sequence[SkillConflict]) -> str:
    """Group conflict facts with one nondestructive remedy."""
    return "\n".join(
        [
            *(conflict.message() for conflict in conflicts),
            "Rename the local skill or use a valid existing skills: or targets: selection "
            "in apm.yml that excludes these conflicts, then run 'apm install'.",
        ]
    )


def _matches_deployed_file(
    integrator: SkillIntegrator, source: Path, target: Path, source_root: Path
) -> bool:
    """Compare the same bytes as copy plus optional Markdown link rewriting."""
    if source.suffix == ".md":
        content = source.read_text(encoding="utf-8")
        resolved, count = integrator.resolve_links(
            content, source, target, preserved_source_root=source_root
        )
        if count:
            return BaseIntegrator.is_content_identical_to_text(
                target, resolved, lf_normalized_deploy=True
            )
    return BaseIntegrator.is_content_identical_to_source(target, source)


def find_unmanaged_conflicts(
    integrator: SkillIntegrator,
    package_info: PackageInfo,
    project_root: Path,
    *,
    targets: list[TargetProfile],
    source_plan: DeployableSourcePlan,
    managed_files: set[str] | None,
    force: bool = False,
    skip_bin: bool = False,
) -> tuple[SkillConflict, ...]:
    """Inspect admitted files without copying, claiming, or choosing a managed winner."""
    if force:
        return ()
    from apm_cli.integration.skill_integrator import (
        _build_deployable_copy_ignore,
        normalize_skill_name,
    )
    from apm_cli.models.apm_package import PackageType

    selection = integrator.selected_skill_sources(package_info)
    roots = [
        *((selection.native_root,) if selection.native_root is not None else ()),
        *selection.children.values(),
    ]
    resolver_ready = False
    managed = managed_files or set()
    conflicts: list[SkillConflict] = []
    collision_diagnostics = DiagnosticCollector()
    affected = tuple(target.name for target in targets if target.supports("skills"))
    allowed_prefixes = BaseIntegrator._get_integration_prefixes(targets=targets)
    # The source plan already traversed and authorized the files. Do not walk
    # the package again or infer authority from a shared destination directory.
    files_by_root: dict[Path, list[Path]] = {root: [] for root in roots}
    for relative in sorted(source_plan.paths):
        source = source_plan.source_root / relative
        for parent in source.parents:
            if parent in files_by_root:
                files_by_root[parent].append(source)
                break
    for source_root, files in files_by_root.items():
        name = normalize_skill_name(source_root.name)
        ignore = _build_deployable_copy_ignore(
            skip_bin=skip_bin,
            source_plan=source_plan,
            exclude_apm=source_root == selection.native_root,
            exclude_apm_yml=(
                source_root == selection.native_root
                and package_info.package_type is PackageType.MARKETPLACE_PLUGIN
            ),
        )
        destinations: dict[Path, tuple[Path, TargetProfile]] = {}
        for target in targets:
            if target.supports("skills"):
                destination = integrator._target_skill_dir(target, project_root, name)
                destinations.setdefault(destination.resolve(), (destination, target))
        for source in files:
            suffix = source.relative_to(source_root)
            parent = source_root
            for part in suffix.parts:
                if part in ignore(str(parent), [part]):
                    break
                parent /= part
            else:
                for destination, target in destinations.values():
                    path = destination / suffix
                    relative_target = deployed_path_entry(path, project_root, [target])
                    if not BaseIntegrator.validate_deploy_path(
                        relative_target, project_root, allowed_prefixes=allowed_prefixes
                    ):
                        from apm_cli.utils.path_security import PathTraversalError

                        raise PathTraversalError(
                            f"Unsafe skill destination '{relative_target}'; refusing deployment."
                        )
                    if not BaseIntegrator.check_collision(
                        path, relative_target, managed, False, diagnostics=collision_diagnostics
                    ):
                        continue
                    if not resolver_ready:
                        integrator.init_link_resolver(package_info, project_root)
                        resolver_ready = True
                    if _matches_deployed_file(integrator, source, path, source_root):
                        continue
                    dependency = package_info.dependency_ref
                    conflicts.append(
                        SkillConflict(
                            name,
                            dependency.get_unique_key() if dependency else "_local",
                            source,
                            relative_target,
                            affected,
                        )
                    )
    return tuple(conflicts)
