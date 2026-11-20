"""Canonical install outcome classification."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePath
from typing import TYPE_CHECKING, NoReturn

from apm_cli.models.results import InstallDisposition, InstallResult

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from apm_cli.install.context import InstallContext


@dataclass(frozen=True)
class InstallCommandOutcome:
    """Adapter-facing command outcome derived from ``InstallResult.disposition``."""

    exit_code: int
    success_summary_allowed: bool


# Characters a POSIX shell takes literally mid-word; '#' starts a comment
# only at the start of a word, so ``owner/repo#v1`` needs no quoting.
_UNQUOTED_ARG = re.compile(r"[\w@%+=:,./#^~-]+")
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


def agent_plugin_target_excluded_count(diagnostics: object | None) -> int:
    """Return the number of target-excluded Agent Plugin packages."""
    if diagnostics is None:
        return 0
    value = getattr(diagnostics, "agent_plugin_target_excluded_count", 0)
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return 0
    try:
        return int(value)
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
    install_spec: str | None = None,
    user_scope: bool = False,
) -> bool:
    """Record one canonical failure when requested components are unavailable.

    *install_spec* is the ``apm install`` argument that reinstalls *package*;
    when given, the failure ends with one runnable ``apm install`` command
    that selects available components instead of free-text advice, in the
    original scope (``-g`` when *user_scope*).
    """
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
    )
    if install_spec:
        message += _available_components_next_step(
            option=option,
            component=component,
            install_spec=install_spec,
            requested=requested_values,
            available=available_names,
            user_scope=user_scope,
        )
    else:
        message += (
            f"Choose an available {component} or update the package manifest, then reinstall."
        )
    diagnostics.error(message, package=package)
    return False


def _available_components_next_step(
    *,
    option: str,
    component: str,
    install_spec: str,
    requested: tuple[str, ...],
    available: frozenset[str],
    user_scope: bool = False,
) -> str:
    """Return the one runnable install command that selects available components."""
    kept = [
        value
        for value in dict.fromkeys(_component_value(value) for value in requested)
        if value in available
    ]
    selection = kept or sorted(available)[:1]
    argv = ["apm", "install", *(["-g"] if user_scope else []), install_spec]
    for value in selection:
        argv.extend((option, value))
    command = " ".join(
        arg if _UNQUOTED_ARG.fullmatch(arg) and not arg.startswith("#") else shlex.quote(arg)
        for arg in argv
    )
    if not selection:
        return f"Run '{command}' to install the package without {option}."
    noun = component if len(selection) == 1 else f"{component}s"
    return f"Run '{command}' to install the available {noun}."


def result_from_install_context(ctx: InstallContext) -> InstallResult:
    """Build and classify the canonical result carried by an install context."""
    complete_dep_keys = getattr(ctx, "update_plan_complete_dep_keys", None)
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
            active_package_keys=(
                frozenset(complete_dep_keys) if complete_dep_keys is not None else None
            ),
            claude_native_keys=frozenset(getattr(ctx, "claude_native_keys", ())),
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
        or (result.installed_count == 0 and agent_plugin_target_excluded_count(diagnostics) > 0)
        or (has_critical and not force)
    ):
        result.disposition = InstallDisposition.FAILED
    apply_install_command_outcome(result)
    return result


def direct_dependency_failed(ctx: InstallContext) -> bool:
    """Whether a direct dependency failed to resolve or download (#3874)."""
    direct_keys = {dep.get_unique_key() for dep in ctx.all_apm_deps}
    return bool(set(ctx.callback_failures) & direct_keys)


def stop_before_integration(ctx: InstallContext, diagnostics: object) -> InstallResult:
    """End an install whose direct dependency failed, before anything integrates.

    The apply barrier (#3874): a failure with error diagnostics returns the
    failed result; one reported only to the console (a local path that is not
    a package) raises, as a failure found during integration would.
    """
    ctx.diagnostics = diagnostics
    result = result_from_install_context(ctx)
    if result.disposition is InstallDisposition.FAILED:
        return result
    raise_direct_dependency_failure(ctx)


def raise_direct_dependency_failure(ctx: InstallContext) -> NoReturn:
    """Render the diagnostic summary and fail the install on a direct dependency."""
    from .errors import DirectDependencyError

    if ctx.diagnostics and ctx.diagnostics.has_diagnostics:
        ctx.diagnostics.render_summary()
    raise DirectDependencyError(
        "One or more direct dependencies failed validation. Run with --verbose for details."
    )


def uninstall_next_step(dep_ref, scope) -> str:
    """The runnable command that removes a missing declared dependency, in its scope."""
    from apm_cli.core.scope import is_user_scope

    flag = "-g " if is_user_scope(scope) else ""
    return f" Run 'apm uninstall {flag}{shlex.quote(dep_ref.get_identity())}' to remove it from apm.yml."
