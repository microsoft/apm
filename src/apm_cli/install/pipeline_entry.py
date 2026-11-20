"""Install entry routing owner (#4085).

Every package install and its ``--dry-run`` preview enter
``run_install_pipeline`` (through ``InstallService``). A preview runs the same
resolution, policy gate and commit gate, then stops before the first write;
:class:`PipelinePreview` carries what it resolved and decided to
``ProspectiveInstallPlan``, which renders it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apm_cli.models.dependency.reference import DependencyReference


@dataclass(frozen=True)
class PipelinePreview:
    """What ``run_install_pipeline(dry_run=True)`` resolved and decided."""

    dependencies: tuple[DependencyReference, ...]
    resolved_dependencies: tuple[DependencyReference, ...]
    # The commit gate's merged direct + transitive MCP set; None when not collected.
    mcp_dependencies: tuple[Any, ...] | None
    errors: tuple[str, ...] = ()
    declaration_errors: tuple[str, ...] = ()


def record_preview_resolution(ctx: Any, graph: Any) -> None:
    """Record ``--dry-run`` resolution gaps; stop where install would refuse."""
    from apm_cli.install.errors import PreviewIncomplete

    ctx.preview_errors.extend(graph.resolution_errors)
    ctx.preview_errors.extend(graph.dependency_tree.unavailable_subtrees)
    ctx.preview_declaration_errors.extend(graph.declaration_errors)
    if graph.circular_dependencies:
        ctx.preview_errors.append("Circular dependencies prevent a complete install preview")
    if graph.resolution_errors or graph.circular_dependencies:
        raise PreviewIncomplete()


def pipeline_preview(run_pipeline: Any, logger: Any) -> PipelinePreview | None:
    """Return the install pipeline's ``--dry-run`` plan, or None when none was built.

    A source the preview cannot reach is reported as an incomplete preview,
    not a failure, so ``--dry-run`` keeps its exit code.
    """
    import click

    from apm_cli.install.errors import (
        AuthenticationError,
        FrozenInstallError,
        InstallFailureAlreadyRendered,
        PolicyViolationError,
    )
    from apm_cli.install.insecure_policy import InsecureDependencyPolicyError

    try:
        result = run_pipeline()
    except (
        AuthenticationError,
        FrozenInstallError,
        InsecureDependencyPolicyError,
        InstallFailureAlreadyRendered,
        PolicyViolationError,
        click.UsageError,
    ):
        raise
    except Exception as exc:  # unreachable source, network, or resolver failure
        message = str(exc) or type(exc).__name__
        logger.verbose_detail(f"Dry-run resolution stopped: {message}")
        return PipelinePreview(
            dependencies=(), resolved_dependencies=(), mcp_dependencies=None, errors=(message,)
        )
    preview = getattr(result, "preview", None)
    return preview if isinstance(preview, PipelinePreview) else None


def preview_declared_policy(ctx: Any, plan: Any, logger: Any) -> None:
    """Preview org policy on the declared dependencies when no pipeline ran."""
    from apm_cli.policy.install_preflight import run_policy_preflight

    run_policy_preflight(
        project_root=ctx.project_root,
        apm_deps=list(plan.selected_apm_dependencies),
        mcp_deps=list(plan.selected_mcp_dependencies) or None,
        no_policy=ctx.no_policy,
        logger=logger,
        dry_run=True,
    )
