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


_CHANGED_COUNTER_KEYS = (
    "prompts",
    "agents",
    "skills",
    "sub_skills",
    "instructions",
    "commands",
    "hooks",
    "canvases",
)


def integration_changed_files(result: dict) -> int:
    """Return how many deployed files one package's integration changed (#4184).

    ``integrate_package_primitives`` is the single owner of this count: it
    records ``files_changed`` once every primitive kind ran. Early-exit results
    (no targets, refused packages) fall back to the per-kind counters, which
    count only fresh writes, never adopted or unchanged files.
    """
    changed = result.get("files_changed")
    if isinstance(changed, int):
        return changed
    return sum(int(result.get(key, 0) or 0) for key in _CHANGED_COUNTER_KEYS)


def package_install_count(result: dict | None, *, fetched: bool) -> int:
    """Return 1 when one package counts as installed this run, else 0 (#4184).

    A package counts when its bytes were freshly fetched, when integration
    changed any deployed file, or when it was natively registered (an Agent
    Plugin deploys no primitives by design). A converged package counts zero.
    """
    if fetched:
        return 1
    if not result:
        return 0
    return int(integration_changed_files(result) > 0 or bool(result.get("native_plugin")))


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
    retry_args: tuple[str, ...] = (),
) -> bool:
    """Record one canonical failure when requested components are unavailable.

    *install_spec* is the ``apm install`` argument that reinstalls *package*;
    when given, the failure ends with one runnable ``apm install`` command
    that selects available components instead of free-text advice.
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
            retry_args=retry_args,
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
    retry_args: tuple[str, ...] = (),
) -> str:
    """Return the one runnable install command that selects available components."""
    kept = [
        value
        for value in dict.fromkeys(_component_value(value) for value in requested)
        if value in available
    ]
    selection = kept or sorted(available)[:1]
    argv = ["apm", "install", install_spec]
    for value in selection:
        argv.extend((option, value))
    argv.extend(retry_args)
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
            mcp_policy_enforced=bool(getattr(ctx, "mcp_policy_enforced", False)),
            legacy_adopted=int(getattr(ctx, "legacy_adopted", 0)),
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


def local_bundle_update_next_step(names, file_count: int, scope) -> str:
    """Explain why 'apm update' leaves local bundles untouched and how to update one (#4318)."""
    from apm_cli.core.scope import is_user_scope

    if names:
        noun = "Local bundle" if len(names) == 1 else "Local bundles"
        subject = f"{noun} {', '.join(names)}"
    else:
        subject = f"Local bundle content ({file_count} file(s))"
    from apm_cli.core.remediation import LOCAL_BUNDLE_UPDATE

    step = LOCAL_BUNDLE_UPDATE
    if is_user_scope(scope):
        step = step.with_command("apm install -g <newer-bundle-file>")
    return f"{subject} not updated: 'apm update' refreshes apm.yml dependencies only. {step.sentence()}"


def local_bundle_parked_next_step(slug: str, exec_types, scope) -> str:
    """Name a local bundle whose executables stay parked and how to approve them (#4441).

    Same remedy as ``apm approve --pending``: approve the exact key under
    ``executables.allow``, then reinstall the bundle.
    """
    from apm_cli.core.remediation import LOCAL_BUNDLE_EXECUTABLES_PARKED
    from apm_cli.core.scope import is_user_scope
    from apm_cli.security.trust import approve_command

    pending = approve_command("--pending", user_scope=is_user_scope(scope))
    remedy = LOCAL_BUNDLE_EXECUTABLES_PARKED.with_command(pending)
    return (
        f"Local bundle '{slug}': {', '.join(exec_types)} parked, awaiting approval. "
        f"{remedy.sentence()}"
    )


def frozen_verified_line(result) -> str:
    """The closing line of a successful --frozen replay."""
    adopted = int(getattr(result, "legacy_adopted", 0) or 0)
    if not adopted:
        return "Deployed files verified against apm.lock.yaml."
    from apm_cli.core.remediation import LEGACY_RENDER_MIGRATE

    return (
        f"Adopted {adopted} file(s) deployed by APM before 0.37, unchanged as committed. "
        f"{LEGACY_RENDER_MIGRATE.sentence()}"
    )
