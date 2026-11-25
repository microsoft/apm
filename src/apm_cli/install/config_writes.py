"""Config files an install writes through server ownership (#4956).

MCP server entries land in a runtime's shared config (``.kiro/settings/mcp.json``,
``.gemini/settings.json``) under an ownership record, never a ``deployed_files``
claim. ``unmanaged_files`` judges them anyway, so the install projects the
files it would create or edit and hands them to the one unmanaged-files
collector (``policy_checks._check_unmanaged_files``) before any write.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from apm_cli.utils.paths import portable_relpath


def _project_relative(path: Path, project_root: Path) -> str | None:
    """The project-relative POSIX path of *path*, or ``None`` outside the root."""
    # Only the parent resolves: a symlinked config file stays at its own path.
    parent = portable_relpath(path.parent, project_root)
    if parent.startswith("/") or parent.startswith("..") or ":" in parent[:3]:
        return None
    return path.name if parent == "." else f"{parent}/{path.name}"


def mcp_config_files(
    runtimes: Iterable[str],
    *,
    project_root: Path,
    user_scope: bool,
    materialized_roots: Iterable[str] = (),
) -> tuple[str, ...]:
    """Project-relative MCP config files the install would create or edit for *runtimes*.

    A project-scope config in a directory that does not exist yet is skipped
    unless the install materializes that root (*materialized_roots*): those
    runtimes are opt-in and the install writes nothing there otherwise.
    """
    from apm_cli.factory import ClientFactory

    found: dict[str, None] = {}
    materialized = {root.strip("/") for root in materialized_roots}
    for runtime in runtimes:
        try:
            client = ClientFactory.create_client(
                runtime, project_root=project_root, user_scope=user_scope
            )
            config_path = Path(client.peek_config_path())
        except (ValueError, OSError, TypeError):
            continue
        rel = _project_relative(config_path, project_root)
        if rel is None:
            continue
        top = rel.split("/", 1)[0]
        if (
            "/" in rel
            and not user_scope
            and top not in materialized
            and not (project_root / top).is_dir()
        ):
            continue
        found[rel] = None
    return tuple(found)


def resolved_mcp_runtimes(*, project_root: Path, user_scope: bool, **targets: Any) -> list[str]:
    """The runtimes the MCP stage resolves from *targets*, resolved without writing."""
    from apm_cli.install.mcp.collision_preflight import _Silent
    from apm_cli.integration.mcp_integrator_install import resolve_mcp_targets

    return list(
        resolve_mcp_targets(
            **targets,
            project_root=project_root,
            user_scope=user_scope,
            logger=_Silent(),
            verbose=False,
            console=None,
            materialize=False,
        ).runtime_list()
        or []
    )


def writing_runtimes(runtimes: Iterable[str], mcp_deps: Iterable[Any], lockfile: Any) -> list[str]:
    """The *runtimes* the MCP writer reaches for *mcp_deps*, as it narrows them.

    A dependency's ``targets:`` narrows the run's runtimes through
    ``mcp_dependency_runtimes``. A runtime APM already owns servers in stays
    listed, as reconciliation may edit it.
    """
    from apm_cli.integration.mcp_integrator_install import mcp_dependency_runtimes

    active = list(runtimes)
    reached = {rt for dep in mcp_deps for rt in mcp_dependency_runtimes(dep, active)}
    owned = {
        runtime
        for runtime, servers in (getattr(lockfile, "mcp_target_servers", None) or {}).items()
        if servers
    }
    return [runtime for runtime in active if runtime in reached or runtime in owned]


def _materialized_roots(target_decision: Any, user_scope: bool) -> list[str]:
    """Target roots the MCP stage creates for *target_decision*, as it materializes them."""
    from apm_cli.integration.targets import KNOWN_TARGETS

    names = getattr(target_decision, "canonical_targets", None)
    if user_scope or names is None:
        return []
    return [KNOWN_TARGETS[name].root_dir for name in names if name in KNOWN_TARGETS]


def projected_mcp_config_files(
    *,
    project_root: Path,
    user_scope: bool,
    mcp_deps: Iterable[Any] | None = None,
    lockfile: Any = None,
    **targets: Any,
) -> tuple[str, ...]:
    """MCP config files an install with these *targets* and *mcp_deps* would write."""
    runtimes = resolved_mcp_runtimes(project_root=project_root, user_scope=user_scope, **targets)
    if mcp_deps is not None:
        runtimes = writing_runtimes(runtimes, mcp_deps, lockfile)
    return mcp_config_files(
        runtimes,
        project_root=project_root,
        user_scope=user_scope,
        materialized_roots=_materialized_roots(targets.get("target_decision"), user_scope),
    )


def mcp_entry_writer(scope: Any, runtime: Any, exclude: Any, target_decision: Any) -> Any:
    """A callable listing the config files ``apm install --mcp`` would write."""
    from apm_cli.core.scope import get_deploy_root, is_user_scope

    return lambda: projected_mcp_config_files(
        project_root=get_deploy_root(scope),
        user_scope=is_user_scope(scope),
        runtime=runtime,
        exclude=exclude,
        apm_config=None,
        explicit_target=target_decision.value,
        target_decision=target_decision,
        scope=scope,
    )


def pipeline_config_files(ctx: Any) -> tuple[str, ...]:
    """MCP config files the install pipeline's MCP stage would write for *ctx*."""
    from apm_cli.core.scope import is_user_scope
    from apm_cli.install.lsp.integration import lsp_target_config

    if not ctx.preview_mcp_dependencies or ctx.target_decision is None:
        return ()
    filters = ctx.lsp_preflight
    return projected_mcp_config_files(
        project_root=ctx.project_root,
        user_scope=is_user_scope(ctx.scope),
        runtime=filters.runtime if filters else getattr(ctx, "runtime", None),
        exclude=filters.exclude if filters else getattr(ctx, "exclude", None),
        apm_config=lsp_target_config(ctx.apm_package),
        explicit_target=ctx.target_decision.value,
        target_decision=ctx.target_decision,
        scope=ctx.scope,
        mcp_deps=ctx.preview_mcp_dependencies,
        lockfile=getattr(ctx, "existing_lockfile", None),
    )
