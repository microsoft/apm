"""Primitive dispatch pipeline for a single package.

Provides ``integrate_package_primitives`` and its private helpers.
The public symbol is re-exported from ``apm_cli.install.services`` so
all existing import paths and ``mock.patch`` seams continue to work.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.integration.skill_integrator.opts import SkillOpts as _SkillOpts

from ._prim_dispatch import (
    _collect_and_log_skills,
    _dispatch_non_skill_primitives,
    _DispatchCtx,
    _maybe_emit_cowork_warning,
    _SkillLogCtx,
    _WarnCtx,
)

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.context import InstallContext
    from apm_cli.utils.diagnostics import DiagnosticCollector

# Shadow builtins defensively: see ``__init__`` module-level comment.
set = builtins.set
list = builtins.list
dict = builtins.dict

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _IntegratorSet:
    """Bundled integrator arguments for :func:`integrate_package_primitives`."""

    prompt_integrator: Any
    agent_integrator: Any
    skill_integrator: Any
    instruction_integrator: Any


def integrate_package_primitives(
    package_info: Any,
    project_root: Path,
    *,
    targets: Any,
    integrators: _IntegratorSet,
    **kwargs,
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
    command_integrator: Any = kwargs.get("command_integrator")
    hook_integrator: Any = kwargs.get("hook_integrator")
    force: bool = kwargs.get("force", False)
    managed_files: Any = kwargs.get("managed_files")
    diagnostics: DiagnosticCollector = kwargs.get("diagnostics")
    package_name: str = kwargs.get("package_name", "")
    logger: InstallLogger | None = kwargs.get("logger")
    skill_subset: tuple | None = kwargs.get("skill_subset")
    ctx: InstallContext | None = kwargs.get("ctx")
    scratch_root: Path | None = kwargs.get("scratch_root")
    prompt_integrator = integrators.prompt_integrator
    agent_integrator = integrators.agent_integrator
    skill_integrator = integrators.skill_integrator
    instruction_integrator = integrators.instruction_integrator
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

    # Drift-replay safety guard (#drift): assert project_root is within
    # scratch_root when the caller redirects integration to an isolated dir.
    if scratch_root is not None:
        from apm_cli.utils.path_security import ensure_path_within

        scratch_root = Path(scratch_root).resolve()
        ensure_path_within(Path(project_root).resolve(), scratch_root)

    _maybe_emit_cowork_warning(
        package_info,
        package_name,
        targets,
        _WarnCtx(ctx=ctx, logger=logger, diagnostics=diagnostics),
    )

    def _log_integration(msg: str) -> None:
        if logger:
            logger.tree_item(msg)

    _verbose = bool(getattr(ctx, "verbose", False)) if ctx is not None else False

    _INTEGRATOR_KWARGS: dict[str, Any] = {
        "prompts": prompt_integrator,
        "agents": agent_integrator,
        "commands": command_integrator,
        "instructions": instruction_integrator,
        "hooks": hook_integrator,
        "skills": skill_integrator,
    }

    _dispatch_non_skill_primitives(
        _DispatchCtx(
            dispatch=_dispatch,
            integrator_kwargs=_INTEGRATOR_KWARGS,
            targets=targets,
            package_info=package_info,
            project_root=project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
            deployed=deployed,
            result=result,
            log_fn=_log_integration,
            verbose=_verbose,
        )
    )

    _collect_and_log_skills(
        _SkillLogCtx(
            skill_integrator=skill_integrator,
            package_info=package_info,
            project_root=project_root,
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            targets=targets,
            skill_subset=skill_subset,
            result=result,
            deployed=deployed,
            log_fn=_log_integration,
            verbose=_verbose,
        )
    )

    _total_integrated = (
        result["prompts"]
        + result["agents"]
        + result["commands"]
        + result["instructions"]
        + result["hooks"]
        + result["skills"]
        + result["sub_skills"]
    )
    if _total_integrated == 0:
        _log_integration("  |-- (files unchanged)")

    return result


__all__ = ["_IntegratorSet", "_WarnCtx", "integrate_package_primitives"]
