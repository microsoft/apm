"""Helper functions for the ``apm update`` command."""

from __future__ import annotations

import os
import sys

import click

from ..install.errors import (
    AuthenticationError,
    DirectDependencyError,
    FrozenInstallError,
    PolicyViolationError,
)
from ..install.plan import UpdatePlan, render_plan_text
from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success


def _stdin_is_tty() -> bool:
    """Return True only when stdin is connected to a real terminal."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _prepare_update_runtime(project_root):
    """Change directory when needed and load the heavy install modules."""
    if project_root is not None and project_root != os.getcwd():
        os.chdir(project_root)
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        _rich_info(
            "'apm update' refreshes APM dependencies. Use 'apm self-update' to update the CLI binary.",
            symbol="info",
        )

    try:
        from apm_cli.commands.install import _install_apm_dependencies
        from apm_cli.core.scope import InstallScope
        from apm_cli.models.apm_package import APMPackage
    except ImportError as exc:  # pragma: no cover -- defensive
        _rich_error(f"APM dependency system not available: {exc}")
        sys.exit(1)
    return _install_apm_dependencies, InstallScope, APMPackage


def _load_update_package(APMPackage):
    """Load the current project's apm.yml for update planning."""
    from pathlib import Path

    try:
        apm_package = APMPackage.from_apm_yml(Path("apm.yml"))
    except (FileNotFoundError, ValueError) as exc:
        _rich_error(f"Failed to parse apm.yml: {exc}")
        sys.exit(1)

    if apm_package.has_apm_dependencies() or apm_package.get_dev_apm_dependencies():
        return apm_package

    _rich_success("No APM dependencies declared in apm.yml -- nothing to update.")
    return None


def _build_plan_callback(
    plan_state: dict[str, UpdatePlan | bool], *, assume_yes: bool, dry_run: bool, verbose: bool
):
    """Build the update plan callback used by the install pipeline."""

    def _plan_callback(plan: UpdatePlan) -> bool:
        plan_state["plan"] = plan
        if not plan.has_changes:
            _rich_success(
                "All dependencies already at their latest matching refs.",
                symbol="check",
            )
            return False

        rendered = render_plan_text(plan, verbose=verbose)
        if rendered:
            _rich_echo(rendered)
            _rich_echo("")
        if dry_run:
            _rich_info(
                "Dry run: no changes applied. Re-run without --dry-run to update.",
                symbol="info",
            )
            return False
        if assume_yes:
            plan_state["proceeded"] = True
            return True
        if not _stdin_is_tty():
            _rich_error(
                "Cannot prompt for confirmation in non-interactive shell. "
                "Re-run with --yes to apply, or --dry-run to preview."
            )
            sys.exit(1)

        proceed = click.confirm("Apply these changes?", default=False, show_default=True)
        plan_state["proceeded"] = proceed
        if not proceed:
            _rich_info("No changes applied.", symbol="info")
        return proceed

    return _plan_callback


def _run_update_install(
    _install_apm_dependencies,
    InstallScope,
    apm_package,
    logger,
    plan_callback,
    *,
    verbose: bool,
    target: str | list[str] | None = None,
):
    """Execute the update install flow with a callback-backed plan."""
    try:
        return _install_apm_dependencies(
            apm_package,
            update_refs=True,
            verbose=verbose,
            scope=InstallScope.PROJECT,
            logger=logger,
            plan_callback=plan_callback,
            target=target,
        )
    except FrozenInstallError as exc:
        _rich_error(str(exc))
        for reason in exc.reasons:
            _rich_echo(reason)
        sys.exit(1)
    except AuthenticationError as exc:
        _rich_error(str(exc))
        if exc.diagnostic_context:
            _rich_echo(exc.diagnostic_context)
        sys.exit(1)
    except (DirectDependencyError, PolicyViolationError) as exc:
        _rich_error(str(exc))
        sys.exit(1)
    except click.UsageError:
        raise
    except Exception as exc:
        _rich_error(f"Error updating dependencies: {exc}")
        if not verbose:
            _rich_info("Run with --verbose for detailed diagnostics.")
        sys.exit(1)


def _render_update_result(plan_state: dict[str, UpdatePlan | bool], result) -> None:
    """Render the post-update success message when the plan proceeded."""
    plan = plan_state.get("plan")
    if plan is None or not isinstance(plan, UpdatePlan):
        return
    if not plan_state.get("proceeded"):
        return

    installed = getattr(result, "installed_count", 0)
    if installed:
        _rich_success(f"Updated {installed} APM dependencies.")
    else:
        _rich_success("Update applied.")
