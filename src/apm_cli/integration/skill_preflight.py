"""Read-only skill source routing and unmanaged-file collision preflight."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from apm_cli.core.remediation import SKILL_CONFLICT_UNMANAGED
from apm_cli.install.deployed_paths import deployed_path_entry, refuse_symlinked_deploy
from apm_cli.integration.base_integrator import (
    BaseIntegrator,
    recovery_directory,
    untrusted_baseline_message,
)
from apm_cli.integration.skill_adoption import (
    installing_commit,
    is_skill_dir_identical_to_source,
    skill_tree_baselines,
)
from apm_cli.integration.skill_support import skill_dir_has_content
from apm_cli.models.dependency.subsets import skill_subset_filter_tokens, skill_subset_selects_all
from apm_cli.utils.diagnostics import DiagnosticCollector, printable_ascii_text
from apm_cli.utils.paths import relative_to

if TYPE_CHECKING:
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.integration.skill_integrator import SkillIntegrator
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.models.apm_package import PackageInfo
    from apm_cli.models.dependency.subsets import SkillTargetSelection


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
    from apm_cli.utils.realpath import resolve_path

    root = resolve_path(package_info.install_path)
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


def planned_skill_files(
    package_info: PackageInfo,
    target: TargetProfile,
    project_root: Path,
    source_plan: DeployableSourcePlan | None,
    skill_pairing: SkillTargetSelection | None = None,
) -> dict[Path, Path]:
    """Map each source file a promoted skill deploys for *target* to its destination.

    Read-only projection of the route, name filter, destination and copy filter
    skill deployment uses, so other primitives can link to the deployed copy
    instead of gitignored ``apm_modules/`` (#5523). A plugin's normalized
    ``.apm/skills/<name>`` mirror is keyed too: in-package links resolve there.
    Unknown plans, native skills and executables yield no entry (the caller
    keeps its fallback), so the map never names a file deployment skips.
    """
    from apm_cli.integration.skill_bundle_routes import promoted_skill_name
    from apm_cli.integration.skill_integrator import SkillIntegrator
    from apm_cli.integration.skill_support import _build_deployable_copy_ignore
    from apm_cli.models.apm_package import PackageType
    from apm_cli.utils.realpath import resolve_path

    if source_plan is None or not target.supports("skills"):
        return {}
    selection = selected_skill_sources(package_info)
    if selection.route not in ("bundle", "subskills"):
        return {}
    # The effective pairing deployment receives, not the raw dependency one.
    name_filter = SkillIntegrator._paired_name_filter(
        source_plan.selected_skill_names, skill_pairing, (target.name,)
    )
    skills_root = (
        project_root / target.skills_rel_root
        if selection.route == "bundle"
        else SkillIntegrator._target_skills_root(target, project_root)
    )
    mirror_root = resolve_path(package_info.install_path) / ".apm" / "skills"
    is_plugin = package_info.package_type is PackageType.MARKETPLACE_PLUGIN
    ignore = _build_deployable_copy_ignore(skip_bin=True, source_plan=source_plan)
    walker = BaseIntegrator()
    planned: dict[Path, Path] = {}
    for key, source in selection.children.items():
        if not (source / "SKILL.md").is_file():
            continue
        if name_filter is not None and source.name not in name_filter:
            continue
        destination = skills_root / promoted_skill_name(source.name)
        source = source.resolve()
        aliases = (source, mirror_root / key) if is_plugin else (source,)
        # The same admitted-member walk the skill copy performs.
        for member in walker.admitted_source_members(source, ignore):
            relative = member.relative_to(source)
            for alias in aliases:
                planned[alias / relative] = destination / relative
    return planned


def preflight_skill_selection(
    package_info: PackageInfo,
    *,
    skill_subset: tuple[str, ...] | None,
    source_plan: DeployableSourcePlan | None,
    diagnostics: DiagnosticCollector | None = None,
    logger=None,
) -> bool:
    """Report an unmatched skill selection before any primitive of the package writes.

    Uses the same route and availability that skill deployment consumes, so a
    selection refused here is exactly one deployment would also refuse (#3373).
    """
    from apm_cli.integration.skill_integrator import SkillIntegrator

    selection = selected_skill_sources(package_info)
    if selection.route not in ("bundle", "subskills"):
        return True
    name_filter = (
        source_plan.selected_skill_names
        if source_plan is not None
        else skill_subset_filter_tokens(skill_subset)
    )
    available = frozenset(selection.children)
    dependency = package_info.dependency_ref
    package_key = (
        dependency.get_unique_key() if dependency is not None else package_info.install_path.name
    )
    # #3204 holds per declaring edge: a merged union must not hide one
    # parent's selection that matches nothing (#3735).
    for edge in getattr(dependency, "selection_contributors", ()) or ():
        edge_filter = skill_subset_filter_tokens(edge.skill_subset)
        if SkillIntegrator._skill_filter_misses_available(
            edge_filter, available, selects_all=skill_subset_selects_all(edge.skill_subset)
        ):
            SkillIntegrator._error_no_skill_filter_match(
                available,
                tuple(edge.skill_subset or ()),
                package_key,
                diagnostics=diagnostics,
                logger=logger,
                declared_by=edge.declared_by() if edge.parent_chain else None,
            )
            return False
    if not SkillIntegrator._skill_filter_misses_available(
        name_filter, available, selects_all=skill_subset_selects_all(skill_subset)
    ):
        return True
    SkillIntegrator._error_no_skill_filter_match(
        available,
        tuple(skill_subset or ()),
        package_key,
        diagnostics=diagnostics,
        logger=logger,
    )
    return False


@dataclass(frozen=True)
class SkillConflict:
    """A differing file at a selected physical skill destination.

    ``untrusted`` marks a managed file that materialization would refuse to
    replace because it has no trustworthy local deployment baseline (#3625).
    """

    skill: str
    package: str
    source: Path
    path: str
    targets: tuple[str, ...]
    untrusted: bool = False
    directory: str | None = None
    user_scope: bool = False

    def message(self) -> str:
        """Name the source, local file and affected targets in ordinary output."""
        if self.untrusted:
            return printable_ascii_text(
                untrusted_baseline_message(self.path, self.directory, user_scope=self.user_scope)
            )
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
            SKILL_CONFLICT_UNMANAGED.sentence(),
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
    """Inspect admitted files without copying, claiming, or choosing a managed winner.

    Managed files that materialization would refuse for lack of a trustworthy
    baseline are reported as ``untrusted`` so no package writes before them.
    """
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
    dependency = package_info.dependency_ref
    owner = dependency.get_unique_key() if dependency else "."
    session = integrator._materialization_session
    user_scope = integrator.install_user_scope()
    replay_owners = integrator._replay_owners
    adopted: dict[tuple[Path, Path], dict[Path, str]] = {}
    unchanged: dict[tuple[Path, Path], bool] = {}
    left_alone: dict[tuple[Path, Path], bool] = {}

    def user_skill_dir(source_root: Path, destination: Path, target: TargetProfile) -> bool:
        """Mirror materialization, which never writes into an unclaimed populated skill dir.

        Members of such a directory (for example ``apm install <bundle>``
        output) are skipped there, so they are not replacement refusals here.
        """
        key = (source_root, destination)
        if key not in left_alone:
            left_alone[key] = (
                skill_dir_has_content(destination)
                and BaseIntegrator.check_collision(
                    destination,
                    deployed_path_entry(destination, project_root, [target]),
                    managed,
                    False,
                    diagnostics=DiagnosticCollector(),
                )
                and integrator._ownership_maps(project_root)[0].get(name) != owner
                and not is_skill_dir_identical_to_source(source_root, destination)
            )
        return left_alone[key]

    def claimed(path: Path, destination: Path) -> bool:
        entry = deployed_path_entry(path, project_root, targets)
        return entry in managed or (
            path == destination / "SKILL.md"
            and deployed_path_entry(destination, project_root, targets) in managed
        )

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
                try:
                    resolved = destination.resolve()
                except (OSError, RuntimeError):
                    # A link loop cannot be resolved; it is refused like any link (#5287).
                    refuse_symlinked_deploy(destination, project_root)
                    raise
                destinations.setdefault(resolved, (destination, target))
        for source in files:
            suffix = relative_to(source, source_root)
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
                    unmanaged = BaseIntegrator.check_collision(
                        path, relative_target, managed, False, diagnostics=collision_diagnostics
                    )
                    if not unmanaged and (
                        session is None
                        or not path.is_file()
                        or (
                            replay_owners is not None
                            and replay_owners.get(relative_target, owner) != owner
                        )
                        or session.baseline(path, targets) is not None
                    ):
                        continue
                    if not resolver_ready:
                        integrator.init_link_resolver(package_info, project_root)
                        resolver_ready = True
                    identical = _matches_deployed_file(integrator, source, path, source_root)
                    if unmanaged and identical:
                        continue
                    if not unmanaged:
                        if user_skill_dir(source_root, destination, target):
                            continue
                        key = (source_root, destination)
                        if key not in adopted:
                            # Copies keep identical members only for a raw-identical sub-skill.
                            unchanged[key] = (
                                source_root != selection.native_root
                                and is_skill_dir_identical_to_source(source_root, destination)
                            )
                            adopted[key] = skill_tree_baselines(
                                integrator,
                                source_root,
                                destination,
                                ignore=ignore,
                                owned=partial(claimed, destination=destination),
                                force=False,
                                plan=source_plan,
                                owner=owner,
                                targets=targets,
                                commit=installing_commit(package_info),
                            )
                        if path in adopted[key] or (identical and unchanged[key]):
                            continue
                    conflicts.append(
                        SkillConflict(
                            name,
                            dependency.get_unique_key() if dependency else "_local",
                            source,
                            relative_target,
                            affected,
                            untrusted=not unmanaged,
                            directory=recovery_directory(
                                deployed_path_entry(destination, project_root, [target]),
                                destination,
                                user_scope=user_scope,
                            ),
                            user_scope=user_scope,
                        )
                    )
    return tuple(conflicts)
