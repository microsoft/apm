"""Install entry routing owner (#4085).

Every package install and its ``--dry-run`` preview enter
``run_install_pipeline`` (through ``InstallService``). A preview runs the same
resolution, policy gate and commit gate, then stops before the first write;
:class:`PipelinePreview` carries what it resolved and decided to
``ProspectiveInstallPlan``, which renders it.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
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
    # (marketplace, plugin) pairs the resolver resolved to a package (RA-036).
    resolved_marketplace_plugins: frozenset[tuple[str, str]] = frozenset()


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


@contextlib.contextmanager
def _preview_tool_state() -> Iterator[None]:
    """Child tools (gh credential lookups) keep their state in a throwaway dir.

    ``gh`` records state under ``$XDG_STATE_HOME`` on every call; a preview
    writes nothing under the user's home.
    """
    previous = os.environ.get("XDG_STATE_HOME")
    with tempfile.TemporaryDirectory(prefix="apm-preview-state-") as state:
        os.environ["XDG_STATE_HOME"] = state
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous


def pipeline_preview(run_pipeline: Any, logger: Any) -> PipelinePreview | None:
    """Return the install pipeline's ``--dry-run`` plan, or None when none was built.

    A source the preview cannot reach is reported as an incomplete preview,
    not a failure, so ``--dry-run`` keeps its exit code.
    """
    import click

    from apm_cli.config import config_creation_suppressed
    from apm_cli.install.errors import (
        AuthenticationError,
        FrozenInstallError,
        InstallFailureAlreadyRendered,
        PolicyViolationError,
    )
    from apm_cli.install.insecure_policy import InsecureDependencyPolicyError

    try:
        with config_creation_suppressed(), _preview_tool_state():
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


def enforce_declared_policy(
    project_root: Any, apm_deps: list, mcp_deps: list | None, logger: Any, *, dry_run: bool
) -> None:
    """Apply org policy to declared dependencies when no install pipeline runs (#4458)."""
    from apm_cli.policy.install_preflight import run_policy_preflight

    run_policy_preflight(
        project_root=project_root,
        apm_deps=apm_deps,
        mcp_deps=mcp_deps,
        logger=logger,
        dry_run=dry_run,
    )


def install_pipeline_options(
    ctx: Any, outcome: Any, *, logger: Any, install_mcp: bool, lsp_preflight: Any
) -> dict[str, Any]:
    """The keyword options an install and its ``--dry-run`` preview pass the pipeline.

    The caller adds the frozen replay decision from ``InstallService.frozen_disposition``.
    """
    return dict(
        # Post-install scripts fire after MCP/LSP reconciliation (#4319).
        post_install_after_services=True,
        force=ctx.force,
        parallel_downloads=ctx.parallel_downloads,
        logger=logger,
        scope=ctx.scope,
        auth_resolver=ctx.auth_resolver,
        target=ctx.target or ctx.runtime,
        allow_insecure=ctx.allow_insecure,
        allow_insecure_hosts=ctx.allow_insecure_hosts,
        marketplace_provenance=outcome.marketplace_provenance if ctx.packages and outcome else None,
        protocol_pref=ctx.protocol_pref,
        allow_protocol_fallback=ctx.allow_protocol_fallback,
        no_policy=ctx.no_policy,
        audit_override=ctx.audit_override,
        legacy_skill_paths=ctx.legacy_skill_paths,
        trust_transitive_mcp=ctx.trust_transitive_mcp,
        install_mcp=install_mcp,
        mcp_runtime=ctx.runtime,
        plan_callback=ctx.plan_callback,
        skill_subset=ctx.skill_subset,
        skill_subset_from_cli=ctx.skill_subset_from_cli,
        refresh=ctx.refresh,
        trust_bin=ctx.trust_bin,
        transaction=ctx.transaction,
        lsp_preflight=lsp_preflight,
    )
