"""Package integration services.

The two functions in this module own the *integration template* for a single
package -- looping over the resolved targets, dispatching primitives to their
integrators, accumulating counters, and recording deployed file paths.

Moved here from ``apm_cli.commands.install`` so that the install engine
package owns its own integration logic.  ``commands/install`` keeps thin
underscore-prefixed re-exports for backward compatibility with existing
``@patch`` sites and direct imports.

Design notes
------------
``integrate_local_content()`` calls ``integrate_package_primitives()`` via a
bare-name lookup so that ``@patch`` of either symbol on this module's
namespace intercepts both call paths consistently.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional  # noqa: F401

if TYPE_CHECKING:
    from ..core.command_logger import InstallLogger
    from ..core.scope import InstallScope
    from ..install.context import InstallContext
    from ..utils.diagnostics import DiagnosticCollector


# CRITICAL: Shadow Python builtins that share names with Click commands so
# ``set()`` / ``list()`` / ``dict()`` resolve to the builtins, not Click
# subcommand objects.  ``commands/install`` and ``install/pipeline`` do the
# same dance for the same reason.
set = builtins.set
list = builtins.list
dict = builtins.dict


def _deployed_path_entry(
    target_path: Path,
    project_root: Path,
    targets: Any,
) -> str:
    """Return the lockfile-safe path string for a deployed file.

    For standard targets the entry is ``project_root``-relative.  For
    cowork (dynamic-root) targets the entry uses the synthetic
    ``cowork://`` URI scheme so the lockfile pipeline does not attempt
    a ``Path.relative_to(project_root)`` that would crash.

    Raises
    ------
    RuntimeError
        If the path is outside the project tree and cannot be
        translated to a ``cowork://`` URI via any available target.
    """
    try:
        return target_path.relative_to(project_root).as_posix()
    except ValueError:
        # Path is outside the project tree -- must be a dynamic-root
        # target.  Find the matching target and translate.
        if targets:
            for _t in targets:
                if _t.resolved_deploy_root is not None:
                    from apm_cli.integration.copilot_cowork_paths import to_lockfile_path

                    return to_lockfile_path(target_path, _t.resolved_deploy_root)
        raise RuntimeError(  # noqa: B904
            f"Cannot translate {target_path!r} to a lockfile path: "
            f"path is outside the project tree and no dynamic-root "
            f"target matched. This is a bug — please report it."
        )


def integrate_package_primitives(
    package_info: Any,
    project_root: Path,
    *,
    targets: Any,
    prompt_integrator: Any,
    agent_integrator: Any,
    skill_integrator: Any,
    instruction_integrator: Any,
    command_integrator: Any,
    hook_integrator: Any,
    force: bool,
    managed_files: Any,
    diagnostics: DiagnosticCollector,
    package_name: str = "",
    logger: InstallLogger | None = None,
    scope: InstallScope | None = None,
    skill_subset: tuple | None = None,
    ctx: InstallContext | None = None,
) -> dict:
    """Run the full integration pipeline for a single package.

    Iterates over *targets* (``TargetProfile`` list) and dispatches each
    primitive to the appropriate integrator via the target-driven API.
    Skills are handled separately because ``SkillIntegrator`` already
    routes across all targets internally.

    When *scope* is ``InstallScope.USER``, targets and primitives that
    do not support user-scope deployment are silently skipped.

    When *ctx* is provided, the cowork non-skill primitive warning
    (Amendment 6) is emitted once per install run for packages that
    contain non-skill primitives when the cowork target is active.

    Returns a dict with integration counters and the list of deployed file paths.
    """
    from apm_cli.integration.dispatch import get_dispatch_table

    _dispatch = get_dispatch_table()
    result = {
        "prompts": 0,
        "agents": 0,
        "skills": 0,
        "sub_skills": 0,
        "instructions": 0,
        "commands": 0,
        "hooks": 0,
        "links_resolved": 0,
        "deployed_files": [],
    }

    deployed = result["deployed_files"]

    if not targets:
        return result

    # --- Amendment 6: cowork non-skill primitive warning (once per run) ---
    _cowork_active = any(t.name == "copilot-cowork" for t in targets)
    if _cowork_active and ctx is not None and not ctx.cowork_nonsupported_warned:
        _apm_dir = Path(package_info.install_path) / ".apm"
        _NON_SKILL_DIRS = {
            "agents": "agents",
            "prompts": "prompts",
            "instructions": "instructions",
            "hooks": "hooks",
            # Commands live under ``.apm/prompts/`` and cannot be
            # distinguished from general prompts at directory level
            # without inspecting frontmatter.  Omitted to avoid
            # misleading duplicate warnings.
        }
        _found_types = [
            ptype
            for ptype, subdir in _NON_SKILL_DIRS.items()
            if (_apm_dir / subdir).is_dir() and any((_apm_dir / subdir).iterdir())
        ]
        if _found_types:
            _pkg_label = package_name or getattr(package_info, "name", "unknown")
            _types_str = ", ".join(sorted(builtins.set(_found_types)))
            _warn_msg = (
                f"copilot-cowork target only supports skills; "
                f"non-skill primitives in {_pkg_label} "
                f"({_types_str}) will not deploy to cowork"
            )
            if logger:
                logger.warning(_warn_msg, symbol="warning")
            diagnostics.warn(_warn_msg)
            ctx.cowork_nonsupported_warned = True

    def _log_integration(msg):
        if logger:
            logger.tree_item(msg)

    _INTEGRATOR_KWARGS = {
        "prompts": prompt_integrator,
        "agents": agent_integrator,
        "commands": command_integrator,
        "instructions": instruction_integrator,
        "hooks": hook_integrator,
        "skills": skill_integrator,
    }

    for _target in targets:
        for _prim_name, _mapping in _target.primitives.items():
            _entry = _dispatch.get(_prim_name)
            if not _entry or _entry.multi_target:
                continue  # skills handled below

            _integrator = _INTEGRATOR_KWARGS[_prim_name]
            _int_result = getattr(_integrator, _entry.integrate_method)(
                _target,
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
            )

            if _int_result.files_integrated > 0:
                result[_entry.counter_key] += _int_result.files_integrated
                _effective_root = _mapping.deploy_root or _target.root_dir
                _deploy_dir = (
                    f"{_effective_root}/{_mapping.subdir}/"
                    if _mapping.subdir
                    else f"{_effective_root}/"
                )
                if _prim_name == "instructions" and _mapping.format_id in (
                    "cursor_rules",
                    "claude_rules",
                ):
                    _label = "rule(s)"
                elif _prim_name == "instructions":
                    _label = "instruction(s)"
                elif _prim_name == "hooks":
                    if _target.name == "claude":
                        _deploy_dir = ".claude/settings.json"
                    elif _target.name == "cursor":
                        _deploy_dir = ".cursor/hooks.json"
                    elif _target.name == "codex":
                        _deploy_dir = ".codex/hooks.json"
                    _label = "hook(s)"
                else:
                    _label = _prim_name
                _log_integration(
                    f"  |-- {_int_result.files_integrated} {_label} integrated -> {_deploy_dir}"
                )
            result["links_resolved"] += _int_result.links_resolved
            for tp in _int_result.target_paths:
                deployed.append(_deployed_path_entry(tp, project_root, targets))

    skill_result = skill_integrator.integrate_package_skill(
        package_info,
        project_root,
        diagnostics=diagnostics,
        managed_files=managed_files,
        force=force,
        targets=targets,
        skill_subset=skill_subset,
    )
    _skill_target_dirs: set = builtins.set()
    for tp in skill_result.target_paths:
        try:
            rel = tp.relative_to(project_root)
            if rel.parts:
                _skill_target_dirs.add(rel.parts[0])
        except ValueError:
            # Dynamic-root target (copilot-cowork) -- path is outside project tree.
            _skill_target_dirs.add("copilot-cowork")
    _skill_targets = sorted(_skill_target_dirs)
    _skill_target_str = ", ".join(f"{d}/skills/" for d in _skill_targets) or "skills/"
    if skill_result.skill_created:
        result["skills"] += 1
        _log_integration(f"  |-- Skill integrated -> {_skill_target_str}")
    if skill_result.sub_skills_promoted > 0:
        result["sub_skills"] += skill_result.sub_skills_promoted
        _log_integration(
            f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated -> {_skill_target_str}"
        )
    for tp in skill_result.target_paths:
        deployed.append(_deployed_path_entry(tp, project_root, targets))

    return result


def integrate_local_content(
    project_root: Path,
    *,
    targets: Any,
    prompt_integrator: Any,
    agent_integrator: Any,
    skill_integrator: Any,
    instruction_integrator: Any,
    command_integrator: Any,
    hook_integrator: Any,
    force: bool,
    managed_files: Any,
    diagnostics: DiagnosticCollector,
    logger: InstallLogger | None = None,
    scope: InstallScope | None = None,
    ctx: InstallContext | None = None,
) -> dict:
    """Integrate primitives from the project's own .apm/ directory.

    This treats the project root as a synthetic package so that local
    skills, instructions, agents, prompts, hooks, and commands in .apm/
    are deployed to target directories exactly like dependency primitives.

    Only .apm/ sub-directories are processed.  A root-level SKILL.md is
    intentionally ignored (it describes the project itself, not a
    deployable skill).

    Returns a dict with integration counters and deployed file paths,
    same shape as ``integrate_package_primitives()``.
    """
    from ..models.apm_package import APMPackage, PackageInfo, PackageType

    local_pkg = APMPackage(
        name="_local",
        version="0.0.0",
        package_path=project_root,
        source="local",
    )
    local_info = PackageInfo(
        package=local_pkg,
        install_path=project_root,
        package_type=PackageType.APM_PACKAGE,
    )

    return integrate_package_primitives(
        local_info,
        project_root,
        targets=targets,
        prompt_integrator=prompt_integrator,
        agent_integrator=agent_integrator,
        skill_integrator=skill_integrator,
        instruction_integrator=instruction_integrator,
        command_integrator=command_integrator,
        hook_integrator=hook_integrator,
        force=force,
        managed_files=managed_files,
        diagnostics=diagnostics,
        package_name="_local",
        logger=logger,
        scope=scope,
        ctx=ctx,
    )


# Underscore-prefixed aliases for backward compatibility with existing
# imports/patches in tests and elsewhere that use the old names.
_integrate_package_primitives = integrate_package_primitives
_integrate_local_content = integrate_local_content
