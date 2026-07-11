"""Canonical install outcome classification."""

from __future__ import annotations

from pathlib import PurePath
from typing import TYPE_CHECKING

from apm_cli.models.results import InstallDisposition, InstallResult

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from apm_cli.install.context import InstallContext


def diagnostic_error_count(diagnostics: object | None) -> int:
    """Return a defensive integer error count."""
    if diagnostics is None:
        return 0
    try:
        return int(getattr(diagnostics, "error_count", 0))
    except (TypeError, ValueError):
        return 0


def _component_name(value: str) -> str:
    """Return the leaf name used by selective component integration."""
    return PurePath(value.replace("\\", "/")).name or value


def require_requested_components(
    diagnostics: object,
    *,
    option: str,
    component: str,
    requested: Iterable[str],
    available: Collection[str],
    package: str,
) -> bool:
    """Record one canonical failure when requested components are unavailable."""
    requested_values = tuple(str(value) for value in requested)
    available_names = frozenset(str(value) for value in available)
    missing = tuple(
        value for value in requested_values if _component_name(value) not in available_names
    )
    if not missing:
        return True

    available_display = ", ".join(sorted(available_names)) or "(none)"
    qualifier = "matched no declared" if len(missing) == len(requested_values) else "did not match"
    message = (
        f"{option} {qualifier} {component}s in '{package}'. "
        f"Requested: {', '.join(missing)}. Available: {available_display}. "
        f"Choose an available {component} or update the package manifest, then reinstall."
    )
    diagnostics.error(message, package=package)
    return False


def result_from_install_context(ctx: InstallContext) -> InstallResult:
    """Build and classify the canonical result carried by an install context."""
    return finalize_install_result(
        InstallResult(
            ctx.installed_count,
            ctx.total_prompts_integrated,
            ctx.total_agents_integrated,
            ctx.diagnostics,
            package_types=dict(ctx.package_types),
        ),
        force=bool(getattr(ctx, "force", False)),
    )


def finalize_install_result(
    result: InstallResult,
    *,
    force: bool,
) -> InstallResult:
    """Classify diagnostics before hooks, transaction completion, or return."""
    if result.disposition in {
        InstallDisposition.CANCELLED,
        InstallDisposition.DRY_RUN,
        InstallDisposition.VALIDATION_FAILED,
    }:
        result.exit_code = 1 if result.disposition is InstallDisposition.VALIDATION_FAILED else 0
        return result
    diagnostics = result.diagnostics
    has_critical = bool(
        diagnostics is not None and getattr(diagnostics, "has_critical_security", False)
    )
    if (
        result.disposition is InstallDisposition.FAILED
        or diagnostic_error_count(diagnostics) > 0
        or (has_critical and not force)
    ):
        result.disposition = InstallDisposition.FAILED
        result.exit_code = 1
    else:
        result.exit_code = 0
    return result
