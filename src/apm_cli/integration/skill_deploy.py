"""Deployment helpers for skill integration."""

from __future__ import annotations

import filecmp
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .skill_naming import normalize_skill_name
from .skill_orchestrate import PackageSkillContext as PackageSkillContext
from .skill_orchestrate import integrate_package_skill as integrate_package_skill
from .skill_plugin import _bin_deploy_denied as _bin_deploy_denied
from .skill_plugin import _copy_plugin_file as _copy_plugin_file
from .skill_plugin import _deploy_bin_files as _deploy_bin_files
from .skill_plugin import _deploy_plugin_bin as _deploy_plugin_bin
from .skill_plugin import _deploy_plugin_manifest as _deploy_plugin_manifest
from .skill_sync import _clean_orphaned_skills as _clean_orphaned_skills
from .skill_sync import _get_lockfile_owned_agent_skills as _get_lockfile_owned_agent_skills
from .skill_sync import _sync_skills_legacy as _sync_skills_legacy
from .skill_sync import _sync_skills_managed_files as _sync_skills_managed_files
from .skill_sync import sync_integration as sync_integration

_log = logging.getLogger("apm_cli.integration.skill_integrator")


@dataclass
class CopySkillContext:
    """Dependencies used by the standalone skill-copy helper."""

    should_install_fn: Callable[[Any], bool]
    validate_name_fn: Callable[[str], tuple[bool, str]]
    normalize_name_fn: Callable[[str], str]
    rewriter_factory: Callable[[], Any]


@dataclass
class NativeSkillTargetContext:
    """Shared state for deploying one native skill to one target."""

    package_path: Path
    skill_name: str
    project_root: Path
    current_key: str | None
    lockfile_native_owners: dict[str, str]
    owned_by: dict[str, str]
    sub_skills_dir: Path
    seen_skill_dirs: set[Path]
    diagnostics: Any
    managed_files: set[str] | None
    force: bool
    logger: Any
    link_rewriter: Any


@dataclass
class SkillBundleTargetContext:
    """Shared state for deploying one skill bundle to one target."""

    skills_dir: Path
    parent_name: str
    owned_by: dict[str, str]
    diagnostics: Any
    managed_files: set[str] | None
    force: bool
    project_root: Path
    logger: Any
    name_filter: set[str] | None
    link_rewriter: Any
    seen_skill_dirs: set[Path]


def _validate_skill_name(name: str) -> tuple[bool, str]:
    """Resolve the public validator lazily to avoid circular imports."""
    from .skill_integrator import validate_skill_name

    return validate_skill_name(name)


def find_instruction_files(package_path: Path) -> list[Path]:
    """Find all instruction files in a package."""
    instruction_files: list[Path] = []
    apm_instructions = package_path / ".apm" / "instructions"
    if apm_instructions.exists():
        instruction_files.extend(apm_instructions.glob("*.instructions.md"))
    return instruction_files


def find_agent_files(package_path: Path) -> list[Path]:
    """Find all agent files in a package."""
    agent_files: list[Path] = []
    apm_agents = package_path / ".apm" / "agents"
    if apm_agents.exists():
        agent_files.extend(apm_agents.glob("*.agent.md"))
    return agent_files


def find_prompt_files(package_path: Path) -> list[Path]:
    """Find all prompt files in a package."""
    prompt_files: list[Path] = []
    if package_path.exists():
        prompt_files.extend(package_path.glob("*.prompt.md"))
    apm_prompts = package_path / ".apm" / "prompts"
    if apm_prompts.exists():
        prompt_files.extend(apm_prompts.glob("*.prompt.md"))
    return prompt_files


def find_context_files(package_path: Path) -> list[Path]:
    """Find all context and memory files in a package."""
    context_files: list[Path] = []
    apm_context = package_path / ".apm" / "context"
    if apm_context.exists():
        context_files.extend(apm_context.glob("*.context.md"))
    apm_memory = package_path / ".apm" / "memory"
    if apm_memory.exists():
        context_files.extend(apm_memory.glob("*.memory.md"))
    return context_files


def _copy_skill_to_target(
    package_info: Any,
    source_path: Path,
    target_base: Path,
    targets: Any,
    context: CopySkillContext,
) -> list[Path]:
    """Copy a skill directory to all active target skills directories."""
    if not context.should_install_fn(package_info):
        return []

    source_skill_md = source_path / "SKILL.md"
    if not source_skill_md.exists():
        return []

    raw_skill_name = source_path.name
    is_valid, _ = context.validate_name_fn(raw_skill_name)
    skill_name = raw_skill_name if is_valid else context.normalize_name_fn(raw_skill_name)

    deployed: list[Path] = []
    seen_skill_dirs: set[Path] = set()
    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(target_base)

    for target in targets:
        if not target.supports("skills"):
            continue
        skills_mapping = target.primitives["skills"]
        effective_root = skills_mapping.deploy_root or target.root_dir
        target_root_dir = target_base / target.root_dir
        if not target.auto_create and not target_root_dir.is_dir():
            continue

        skill_dir = target_base / effective_root / "skills" / skill_name
        from apm_cli.utils.path_security import (
            PathTraversalError,
            ensure_path_within,
            validate_path_segments,
        )

        validate_path_segments(skill_name, context="skill name")
        if skill_dir.is_symlink():
            raise PathTraversalError(
                f"Skill destination {skill_dir} is a symlink -- refusing to deploy"
            )

        resolved_project = target_base.resolve()
        resolved_skill_dir = skill_dir.resolve()
        if not resolved_skill_dir.is_relative_to(resolved_project):
            raise PathTraversalError(
                f"Skill directory '{skill_dir}' resolves to '{resolved_skill_dir}' "
                f"which is outside the project root '{resolved_project}'"
            )
        ensure_path_within(skill_dir, target_base / effective_root / "skills")

        resolved = skill_dir.resolve()
        if resolved in seen_skill_dirs:
            _log.debug("%s -- already deployed, skipping for %s", skill_dir, target.name)
            continue
        seen_skill_dirs.add(resolved)

        skill_dir.parent.mkdir(parents=True, exist_ok=True)
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        rewriter = context.rewriter_factory()
        rewriter._copy_source_skill_tree(source_path, skill_dir)
        rewriter.init_link_resolver(package_info, target_base)
        rewriter._resolve_markdown_links_in_skill_bundle(source_path, skill_dir)
        deployed.append(skill_dir)

    return deployed


def is_skill_dir_identical_to_source(dir_a: Path, dir_b: Path) -> bool:
    """Check if two directory trees have identical file contents."""
    dcmp = filecmp.dircmp(str(dir_a), str(dir_b))
    return _dircmp_equal(dcmp)


def _dircmp_equal(dcmp: Any) -> bool:
    """Recursively check if dircmp shows identical contents."""
    if dcmp.left_only or dcmp.right_only or dcmp.funny_files:
        return False
    _, mismatches, errors = filecmp.cmpfiles(
        dcmp.left, dcmp.right, dcmp.common_files, shallow=False
    )
    if mismatches or errors:
        return False
    return all(_dircmp_equal(sub_dcmp) for sub_dcmp in dcmp.subdirs.values())


def _resolve_markdown_links_in_skill_bundle(
    link_rewriter: Any,
    source_root: Path,
    target_root: Path,
) -> int:
    """Read copied skill markdown from source and write resolved target content."""
    links_resolved = 0
    for target_file in target_root.rglob("*.md"):
        if not target_file.is_file() or target_file.is_symlink():
            continue
        source_file = source_root / target_file.relative_to(target_root)
        if not source_file.is_file() or source_file.is_symlink():
            continue
        content = source_file.read_text(encoding="utf-8")
        resolved, count = link_rewriter.resolve_links(
            content,
            source_file,
            target_file,
            preserved_source_root=source_root,
        )
        if count:
            target_file.write_text(resolved, encoding="utf-8")
            links_resolved += count
    return links_resolved


def _emit_unmanaged_skill_skip(
    sub_name: str,
    rel_path: str,
    parent_name: str,
    diagnostics: Any,
    logger: Any,
) -> None:
    """Emit the existing unmanaged-skill skip warning."""
    message = (
        f"Skipping skill '{sub_name}' -- local skill exists (not managed by APM). "
        "Use 'apm install --force' to overwrite."
    )
    if diagnostics is not None:
        diagnostics.skip(rel_path, package=parent_name)
    elif logger:
        logger.warning(message)
    else:
        try:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(message)
        except ImportError:
            pass


def _emit_sub_skill_overwrite(
    sub_name: str,
    rel_path: str,
    parent_name: str,
    diagnostics: Any,
    logger: Any,
) -> None:
    """Emit the existing sub-skill overwrite warning."""
    if diagnostics is not None:
        diagnostics.overwrite(
            path=rel_path,
            package=parent_name,
            detail=f"Skill '{sub_name}' replaced -- previously from another package",
        )
    elif logger:
        logger.warning(
            f"Sub-skill '{sub_name}' from '{parent_name}' overwrites existing skill at {rel_path}"
        )
    else:
        try:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(
                f"Sub-skill '{sub_name}' from '{parent_name}' overwrites existing skill at {rel_path}"
            )
        except ImportError:
            pass


def _target_rel_prefix(target_skills_root: Path, project_root: Path | None) -> str:
    """Return a project-relative target prefix when possible."""
    if project_root is None:
        return target_skills_root.name
    try:
        return target_skills_root.relative_to(project_root).as_posix()
    except ValueError:
        return target_skills_root.name


def _promote_sub_skills(
    sub_skills_dir: Path,
    target_skills_root: Path,
    parent_name: str,
    *,
    warn: bool = True,
    owned_by: dict[str, str] | None = None,
    diagnostics: Any = None,
    managed_files: set[str] | None = None,
    force: bool = False,
    project_root: Path | None = None,
    logger: Any = None,
    name_filter: set[str] | None = None,
    link_rewriter: Any = None,
) -> tuple[int, list[Path]]:
    """Promote sub-skills from a package skill directory."""
    promoted = 0
    deployed: list[Path] = []
    if not sub_skills_dir.is_dir():
        return promoted, deployed

    rel_prefix = _target_rel_prefix(target_skills_root, project_root)
    for sub_skill_path in sub_skills_dir.iterdir():
        if not sub_skill_path.is_dir() or not (sub_skill_path / "SKILL.md").exists():
            continue
        raw_sub_name = sub_skill_path.name
        if name_filter is not None and raw_sub_name not in name_filter:
            continue
        is_valid, _ = _validate_skill_name(raw_sub_name)
        sub_name = raw_sub_name if is_valid else normalize_skill_name(raw_sub_name)
        target = target_skills_root / sub_name
        rel_path = f"{rel_prefix}/{sub_name}"
        if target.exists():
            if is_skill_dir_identical_to_source(sub_skill_path, target):
                promoted += 1
                deployed.append(target)
                continue

            is_managed = managed_files is not None and rel_path.replace("\\", "/") in managed_files
            prev_owner = (owned_by or {}).get(sub_name)
            is_self_overwrite = prev_owner is not None and prev_owner == parent_name
            if managed_files is not None and not is_managed and not is_self_overwrite and not force:
                _emit_unmanaged_skill_skip(sub_name, rel_path, parent_name, diagnostics, logger)
                continue
            if warn and not is_self_overwrite:
                _emit_sub_skill_overwrite(sub_name, rel_path, parent_name, diagnostics, logger)
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        if link_rewriter is not None:
            link_rewriter._copy_promoted_skill_tree(sub_skill_path, target)
            link_rewriter._resolve_markdown_links_in_skill_bundle(sub_skill_path, target)
        else:
            from apm_cli.security.gate import ignore_non_content

            shutil.copytree(sub_skill_path, target, dirs_exist_ok=True, ignore=ignore_non_content)
        promoted += 1
        deployed.append(target)
    return promoted, deployed


def _build_ownership_maps(project_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Read the lockfile once and build sub-skill and native-skill owner maps."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    owned_by: dict[str, str] = {}
    native_owners: dict[str, str] = {}
    lockfile = LockFile.read(get_lockfile_path(project_root))
    if not lockfile:
        return owned_by, native_owners
    for dep in lockfile.get_package_dependencies():
        short_owner = (dep.virtual_path or dep.repo_url).rsplit("/", 1)[-1]
        unique_key = dep.get_unique_key()
        for deployed_path in dep.deployed_files:
            normalized = deployed_path.rstrip("/").replace("\\", "/")
            skill_name = normalized.rsplit("/", 1)[-1]
            owned_by[skill_name] = short_owner
            if "/skills/" in normalized:
                native_owners[skill_name] = unique_key
    return owned_by, native_owners


def _target_skills_root(target: Any, project_root: Path) -> Path:
    """Return the skills root for a target."""
    if target.resolved_deploy_root is not None:
        return target.resolved_deploy_root
    skills_mapping = target.primitives["skills"]
    effective_root = skills_mapping.deploy_root or target.root_dir
    return project_root / effective_root / "skills"


def _skill_subset_name_filter(skill_subset: tuple[str, ...] | None) -> set[str] | None:
    """Return promotion filter tokens for --skill subset values."""
    if not skill_subset:
        return None
    name_filter: set[str] = set()
    for skill_name in skill_subset:
        raw_name = str(skill_name).strip()
        if not raw_name:
            continue
        normalized_path = raw_name.replace("\\", "/")
        leaf_name = Path(normalized_path).name
        name_filter.add(raw_name)
        name_filter.add(normalized_path)
        if leaf_name:
            name_filter.add(leaf_name)
    return name_filter or None


def _promote_sub_skills_standalone(
    link_rewriter: Any,
    package_info: Any,
    project_root: Path,
    *,
    diagnostics: Any = None,
    managed_files: set[str] | None = None,
    force: bool = False,
    logger: Any = None,
    targets: Any = None,
    skill_subset: Any = None,
) -> tuple[int, list[Path]]:
    """Promote sub-skills from a package that is not itself a skill."""
    link_rewriter.init_link_resolver(package_info, project_root)
    package_path = package_info.install_path
    sub_skills_dir = package_path / ".apm" / "skills"
    if not sub_skills_dir.is_dir():
        return 0, []
    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(project_root)

    parent_name = package_path.name
    owned_by, _ = link_rewriter._build_ownership_maps(project_root)
    name_filter = _skill_subset_name_filter(skill_subset)
    count = 0
    all_deployed: list[Path] = []
    seen_skill_dirs: set[Path] = set()
    for idx, target in enumerate(targets):
        if not target.supports("skills"):
            continue
        is_primary = idx == 0
        target_skills_root = _target_skills_root(target, project_root)
        resolved_root = target_skills_root.resolve()
        if resolved_root in seen_skill_dirs:
            if logger:
                logger.progress(
                    f"{target_skills_root} -- already deployed, skipping for {target.name}",
                    symbol="info",
                )
            continue
        seen_skill_dirs.add(resolved_root)
        target_skills_root.mkdir(parents=True, exist_ok=True)
        n, deployed = _promote_sub_skills(
            sub_skills_dir,
            target_skills_root,
            parent_name,
            warn=is_primary,
            owned_by=owned_by if is_primary else None,
            diagnostics=diagnostics if is_primary else None,
            managed_files=managed_files if is_primary else None,
            force=force,
            project_root=project_root,
            name_filter=name_filter,
            link_rewriter=link_rewriter,
        )
        if is_primary:
            count = n
        all_deployed.extend(deployed)
    return count, all_deployed


def _warn_normalized_skill_name(
    raw_skill_name: str,
    skill_name: str,
    error_msg: str,
    diagnostics: Any,
    logger: Any,
) -> None:
    """Emit the existing normalised skill-name warning."""
    message = f"Skill name '{raw_skill_name}' normalized to '{skill_name}' ({error_msg})"
    if diagnostics is not None:
        diagnostics.warn(message, package=raw_skill_name)
    elif logger:
        logger.warning(message)
    else:
        try:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(message)
        except ImportError:
            pass


def _native_collision_warning(ctx: NativeSkillTargetContext, target_skill_dir: Path) -> None:
    """Emit the existing native-skill collision warning when needed."""
    prev_owner = ctx.lockfile_native_owners.get(
        ctx.skill_name
    ) or ctx.link_rewriter._native_skill_session_owners.get(ctx.skill_name)
    is_self_overwrite = prev_owner is not None and prev_owner == ctx.current_key
    if prev_owner is None or is_self_overwrite:
        return
    try:
        rel_prefix = target_skill_dir.parent.relative_to(ctx.project_root).as_posix()
    except ValueError:
        rel_prefix = "skills"
    rel_path = f"{rel_prefix}/{ctx.skill_name}"
    detail = (
        f"Skill '{ctx.skill_name}' from '{ctx.current_key}' replaced "
        f"'{prev_owner}' -- remove one package to avoid this"
    )
    if ctx.diagnostics is not None:
        ctx.diagnostics.overwrite(
            path=rel_path,
            package=ctx.current_key or ctx.skill_name,
            detail=detail,
        )
    elif ctx.logger:
        ctx.logger.warning(detail)
    else:
        from apm_cli.utils.console import _rich_warning

        _rich_warning(detail)


def _integrate_native_skill_to_target(
    target: Any,
    *,
    is_primary: bool,
    context: NativeSkillTargetContext,
) -> dict[str, Any]:
    """Integrate one native skill target and return aggregate updates."""
    if not target.supports("skills"):
        return {"target_paths": []}

    skills_mapping = target.primitives["skills"]
    if target.resolved_deploy_root is not None:
        target_skill_dir = target.resolved_deploy_root / context.skill_name
        target_skills_root = target.resolved_deploy_root
    else:
        effective_root = skills_mapping.deploy_root or target.root_dir
        target_skills_root = context.project_root / effective_root / "skills"
        target_skill_dir = target_skills_root / context.skill_name

    from apm_cli.utils.path_security import (
        PathTraversalError,
        ensure_path_within,
        validate_path_segments,
    )

    validate_path_segments(context.skill_name, context="skill name")
    if target_skill_dir.is_symlink():
        raise PathTraversalError(
            f"Skill destination {target_skill_dir} is a symlink -- refusing to deploy"
        )
    if target.resolved_deploy_root is None:
        ensure_path_within(target_skill_dir, target_skills_root)

    resolved = target_skill_dir.resolve()
    if resolved in context.seen_skill_dirs:
        if context.logger:
            context.logger.progress(
                f"{target_skill_dir} -- already deployed, skipping for {target.name}",
                symbol="info",
            )
        return {"target_paths": []}
    context.seen_skill_dirs.add(resolved)

    result: dict[str, Any] = {"target_paths": []}
    if is_primary:
        result.update(
            skill_created=not target_skill_dir.exists(),
            skill_updated=target_skill_dir.exists(),
            primary_skill_md=target_skill_dir / "SKILL.md",
        )

    if target_skill_dir.exists():
        if is_primary:
            _native_collision_warning(context, target_skill_dir)
        shutil.rmtree(target_skill_dir)

    target_skill_dir.parent.mkdir(parents=True, exist_ok=True)
    context.link_rewriter._copy_native_skill_tree(context.package_path, target_skill_dir)
    context.link_rewriter._resolve_markdown_links_in_skill_bundle(
        context.package_path, target_skill_dir
    )
    result["target_paths"].append(target_skill_dir)

    if is_primary:
        result["files_copied"] = sum(1 for path in target_skill_dir.rglob("*") if path.is_file())

    _, sub_deployed = _promote_sub_skills(
        context.sub_skills_dir,
        target_skills_root,
        context.skill_name,
        warn=is_primary,
        owned_by=context.owned_by if is_primary else None,
        diagnostics=context.diagnostics if is_primary else None,
        managed_files=context.managed_files if is_primary else None,
        force=context.force,
        project_root=context.project_root,
        logger=context.logger if is_primary else None,
        link_rewriter=context.link_rewriter,
    )
    result["target_paths"].extend(sub_deployed)
    return result


def _integrate_native_skill(
    link_rewriter: Any,
    package_info: Any,
    project_root: Path,
    source_skill_md: Path,
    *,
    diagnostics: Any = None,
    managed_files: set[str] | None = None,
    force: bool = False,
    logger: Any = None,
    targets: Any = None,
) -> dict[str, Any]:
    """Copy a native skill to all active targets and return result fields."""
    link_rewriter.init_link_resolver(package_info, project_root)
    package_path = package_info.install_path
    raw_skill_name = package_path.name
    is_valid, error_msg = _validate_skill_name(raw_skill_name)
    if is_valid:
        skill_name = raw_skill_name
    else:
        skill_name = normalize_skill_name(raw_skill_name)
        _warn_normalized_skill_name(raw_skill_name, skill_name, error_msg, diagnostics, logger)

    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(project_root)

    dep_ref = package_info.dependency_ref
    current_key = dep_ref.get_unique_key() if dep_ref is not None else None
    owned_by, lockfile_native_owners = link_rewriter._build_ownership_maps(project_root)
    context = NativeSkillTargetContext(
        package_path=package_path,
        skill_name=skill_name,
        project_root=project_root,
        current_key=current_key,
        lockfile_native_owners=lockfile_native_owners,
        owned_by=owned_by,
        sub_skills_dir=package_path / ".apm" / "skills",
        seen_skill_dirs=set(),
        diagnostics=diagnostics,
        managed_files=managed_files,
        force=force,
        logger=logger,
        link_rewriter=link_rewriter,
    )

    result: dict[str, Any] = {
        "skill_created": False,
        "skill_updated": False,
        "files_copied": 0,
        "target_paths": [],
        "primary_skill_md": None,
        "sub_skills_promoted": 0,
    }
    for idx, target in enumerate(targets):
        target_result = _integrate_native_skill_to_target(
            target,
            is_primary=idx == 0,
            context=context,
        )
        result["target_paths"].extend(target_result.get("target_paths", []))
        for key in ("skill_created", "skill_updated", "files_copied", "primary_skill_md"):
            if key in target_result:
                result[key] = target_result[key]

    if current_key is not None:
        link_rewriter._native_skill_session_owners[skill_name] = current_key

    primary_root = project_root / ".github" / "skills"
    result["sub_skills_promoted"] = sum(
        1
        for path in result["target_paths"]
        if path.parent == primary_root and path.name != skill_name
    )
    return result


def _integrate_skill_bundle_target(
    target: Any,
    *,
    is_primary: bool,
    context: SkillBundleTargetContext,
) -> dict[str, Any]:
    """Integrate one skill-bundle target and return aggregate updates."""
    if not target.supports("skills"):
        return {"deployed": [], "promoted": 0, "created": False}

    skills_mapping = target.primitives["skills"]
    effective_root = skills_mapping.deploy_root or target.root_dir
    target_skills_root = context.project_root / effective_root / "skills"
    resolved_root = target_skills_root.resolve()
    if resolved_root in context.seen_skill_dirs:
        if context.logger:
            context.logger.progress(
                f"{target_skills_root} -- already deployed, skipping for {target.name}",
                symbol="info",
            )
        return {"deployed": [], "promoted": 0, "created": False}
    context.seen_skill_dirs.add(resolved_root)
    target_skills_root.mkdir(parents=True, exist_ok=True)

    promoted, deployed = _promote_sub_skills(
        context.skills_dir,
        target_skills_root,
        context.parent_name,
        warn=is_primary,
        owned_by=context.owned_by if is_primary else None,
        diagnostics=context.diagnostics if is_primary else None,
        managed_files=context.managed_files if is_primary else None,
        force=context.force,
        project_root=context.project_root,
        logger=context.logger if is_primary else None,
        name_filter=context.name_filter,
        link_rewriter=context.link_rewriter,
    )
    return {"deployed": deployed, "promoted": promoted, "created": is_primary and promoted > 0}


def _integrate_skill_bundle(
    link_rewriter: Any,
    package_info: Any,
    project_root: Path,
    skills_dir: Path,
    *,
    diagnostics: Any = None,
    managed_files: set[str] | None = None,
    force: bool = False,
    logger: Any = None,
    targets: Any = None,
    skill_subset: Any = None,
) -> dict[str, Any]:
    """Promote every skill in a skill bundle's top-level skills directory."""
    link_rewriter.init_link_resolver(package_info, project_root)
    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(project_root)

    owned_by, _ = link_rewriter._build_ownership_maps(project_root)
    context = SkillBundleTargetContext(
        skills_dir=skills_dir,
        parent_name=package_info.install_path.name,
        owned_by=owned_by,
        diagnostics=diagnostics,
        managed_files=managed_files,
        force=force,
        project_root=project_root,
        logger=logger,
        name_filter=_skill_subset_name_filter(skill_subset),
        link_rewriter=link_rewriter,
        seen_skill_dirs=set(),
    )
    total_promoted = 0
    all_deployed: list[Path] = []
    any_created = False
    for idx, target in enumerate(targets):
        target_result = _integrate_skill_bundle_target(
            target,
            is_primary=idx == 0,
            context=context,
        )
        if idx == 0:
            total_promoted = target_result["promoted"]
            any_created = target_result["created"]
        all_deployed.extend(target_result["deployed"])
    return {
        "skill_created": any_created,
        "skill_updated": False,
        "skill_skipped": False,
        "skill_path": None,
        "references_copied": 0,
        "links_resolved": 0,
        "sub_skills_promoted": total_promoted,
        "target_paths": all_deployed,
    }
