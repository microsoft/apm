"""Canonical install outcome classification."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import TYPE_CHECKING

from apm_cli.models.results import InstallDisposition, InstallResult

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from apm_cli.install.context import InstallContext


@dataclass(frozen=True)
class InstallCommandOutcome:
    """Adapter-facing command outcome derived from ``InstallResult.disposition``."""

    exit_code: int
    success_summary_allowed: bool


_SUCCESS_SUMMARY_DISPOSITIONS = frozenset(
    {
        InstallDisposition.SUCCESS,
        InstallDisposition.PARTIAL_SUCCESS,
        InstallDisposition.DRY_RUN,
    }
)


def diagnostic_error_count(diagnostics: object | None) -> int:
    """Return a defensive integer error count."""
    if diagnostics is None:
        return 0
    try:
        return int(getattr(diagnostics, "error_count", 0))
    except (TypeError, ValueError):
        return 0


def install_command_outcome(result: InstallResult) -> InstallCommandOutcome:
    """Return the command outcome owned by ``InstallResult.disposition``."""
    if result.disposition in _SUCCESS_SUMMARY_DISPOSITIONS:
        return InstallCommandOutcome(exit_code=0, success_summary_allowed=True)
    if result.disposition is InstallDisposition.CANCELLED:
        return InstallCommandOutcome(exit_code=0, success_summary_allowed=False)
    return InstallCommandOutcome(exit_code=1, success_summary_allowed=False)


def apply_install_command_outcome(result: InstallResult) -> InstallCommandOutcome:
    """Synchronize ``result.exit_code`` with the canonical disposition outcome."""
    outcome = install_command_outcome(result)
    result.exit_code = outcome.exit_code
    return outcome


def _component_value(value: str) -> str:
    """Return the normalized selectable component path."""
    return PurePath(value.replace("\\", "/")).as_posix()


def missing_requested_components(
    *,
    requested: Iterable[str],
    available: Collection[str],
) -> tuple[str, ...]:
    """Return requested component paths absent from the available path set."""
    available_values = frozenset(_component_value(str(value)) for value in available)
    return tuple(
        requested_value
        for requested_value in (_component_value(str(value)) for value in requested)
        if requested_value not in available_values
    )


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
    available_names = frozenset(_component_value(str(value)) for value in available)
    missing = missing_requested_components(
        requested=requested_values,
        available=available_names,
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
            target_decision=getattr(ctx, "target_decision", None),
            exec_allow_map=getattr(ctx, "exec_allow_map", None),
            exec_allow_resolved=getattr(ctx, "exec_trust_ctx", None) is not None,
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
        apply_install_command_outcome(result)
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
    apply_install_command_outcome(result)
    return result
