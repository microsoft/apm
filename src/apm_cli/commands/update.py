"""``apm update`` -- refresh APM dependencies to the latest matching refs.

This is the package-manager convention popularised by ``cargo update``,
``poetry update``, ``bundle update``, and ``npm update`` -- the verb is
about the dependency graph, not about updating the CLI binary itself.
The CLI self-updater lives at ``apm self-update`` (see
:mod:`apm_cli.commands.self_update`); when this command runs outside an
``apm.yml`` project it forwards to the self-updater as a deprecated
back-compat shim for one release (see ``update()`` below).

What it does
------------
``apm update`` is conceptually equivalent to ``apm install --update``
**plus** an interactive plan-and-confirm gate:

1. Run resolve to discover which deps would change.
2. Render a structured plan (``[~]`` updated, ``[+]`` added,
   ``[-]`` removed) that names every dep, the ref/SHA transition, and
   the deployed files at risk.
3. Prompt ``Apply these changes? [y/N]`` -- default **No**, mirroring
   the security framing in the public response on issue #1203.
4. On ``y``: continue the install pipeline (download + integrate +
   lockfile rewrite).  On ``N`` / ``--dry-run`` / no-TTY: exit cleanly
   with no on-disk mutations.

Flags
-----
* ``--yes``/``-y`` -- skip the prompt (CI / automation).
* ``--dry-run``    -- render the plan and exit without prompting.
* ``--verbose``/``-v`` -- show unchanged deps in the plan and pipeline
  diagnostics.

Other ``apm install`` flags are NOT mirrored here on purpose -- the
update command stays focused on the refresh-and-confirm loop.
``apm install --update`` remains the swiss-army-knife escape hatch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..core.command_logger import InstallLogger
from ..install.errors import (
    AuthenticationError,
    DirectDependencyError,
    FrozenInstallError,
    PolicyViolationError,
)
from ..install.plan import UpdatePlan, render_plan_text
from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success, _rich_warning


def _find_apm_yml(start: Path | None = None) -> Path | None:
    """Walk parent directories from ``start`` (or cwd) to find ``apm.yml``.

    Matches the npm / cargo / poetry ergonomic: a developer running
    ``apm update`` from a subdirectory of their project (``src/``,
    ``docs/``, ``scripts/``) finds the manifest and operates on it,
    rather than getting silently misrouted to the deprecated
    self-update shim.

    The walk stops at the filesystem root or when an ``apm.yml`` is
    found, whichever comes first. Returns the absolute path to the
    ``apm.yml`` file when found; ``None`` when no project root is
    discoverable from ``start`` upward.
    """
    cwd = (start or Path.cwd()).resolve()
    for candidate in (cwd, *cwd.parents):
        manifest = candidate / "apm.yml"
        if manifest.is_file():
            return manifest
    return None


def _stdin_is_tty() -> bool:
    """Return True only when stdin is connected to a real terminal.

    A non-TTY stdin (CI, piped, redirected) means we cannot safely
    prompt for confirmation -- ``apm update`` aborts with guidance to
    re-run with ``--yes``.
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


@click.command(
    name="update",
    help="Refresh APM dependencies to the latest matching refs",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt (for CI / automation)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Render the update plan and exit without changing anything",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Show unchanged deps and detailed pipeline diagnostics",
)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    default=False,
    help="(Deprecated) Forwarded to 'apm self-update --check' when run outside an apm.yml project; rejected inside a project.",
    hidden=True,
)
@click.pass_context
def update(
    ctx: click.Context,
    assume_yes: bool,
    dry_run: bool,
    verbose: bool,
    check_only: bool,
) -> None:
    """Refresh APM dependencies to the latest matching refs.

    Examples:
        apm update              # Resolve, show plan, prompt, then install
        apm update --dry-run    # Show plan only, do not change anything
        apm update --yes        # Skip the prompt (CI-safe)
        apm update --verbose    # Include unchanged deps in the plan
    """
    manifest_path = _find_apm_yml()
    if manifest_path is None:
        # Back-compat shim (one-release): when run outside a project,
        # forward to the renamed self-updater so existing users keep
        # working while we publicise ``apm self-update``.  Removed in
        # the release after this one.
        from apm_cli.commands.self_update import self_update as _self_update_cmd

        _rich_warning(
            "'apm update' refreshes APM dependencies. To update the CLI binary, "
            "use 'apm self-update'. Forwarding for back-compat (deprecated).",
            symbol="warning",
        )
        ctx.invoke(_self_update_cmd, check=check_only)
        return

    if check_only:
        from apm_cli.commands.self_update import self_update as _self_update_cmd

        _rich_warning(
            "'apm update --check' is the deprecated self-updater shim. "
            "Use 'apm update --dry-run' to preview dependency changes, "
            "or 'apm self-update --check' to check for a new CLI binary. "
            "Forwarding for back-compat (deprecated).",
            symbol="warning",
        )
        ctx.invoke(_self_update_cmd, check=True)
        return

    project_root = manifest_path.parent
    if project_root != Path.cwd().resolve():
        _rich_info(
            f"Using apm.yml at {manifest_path} (project root: {project_root})",
            symbol="info",
        )

    _run_dep_update(
        assume_yes=assume_yes,
        dry_run=dry_run,
        verbose=verbose,
        project_root=project_root,
    )


def _run_dep_update(
    *,
    assume_yes: bool,
    dry_run: bool,
    verbose: bool,
    project_root: Path | None = None,
) -> None:
    """Core ``apm update`` flow: resolve, plan, prompt, install.

    When ``project_root`` is provided, the working directory is
    switched to it before running so install pipeline paths
    (``apm.yml``, ``apm.lock.yaml``, deployed primitives) resolve
    against the discovered project root, not the caller's cwd.
    """
    import os

    if project_root is not None and project_root != Path.cwd().resolve():
        os.chdir(project_root)

    # Surface the new semantics to CI users on every invocation: the
    # interactive prompt aborts non-TTY runs anyway, but a banner up
    # front prevents "why did our pipeline break overnight?" tickets
    # from teams whose CI calls 'apm update' assuming it self-updates
    # the CLI binary.
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        _rich_info(
            "'apm update' refreshes APM dependencies. "
            "Use 'apm self-update' to update the CLI binary.",
            symbol="info",
        )

    try:
        from apm_cli.commands.install import _install_apm_dependencies  # local import: heavy module
        from apm_cli.core.scope import InstallScope
        from apm_cli.models.apm_package import APMPackage
    except ImportError as e:  # pragma: no cover -- defensive
        _rich_error(f"APM dependency system not available: {e}")
        sys.exit(1)

    try:
        apm_package = APMPackage.from_apm_yml(Path("apm.yml"))
    except (FileNotFoundError, ValueError) as e:
        _rich_error(f"Failed to parse apm.yml: {e}")
        sys.exit(1)

    if not apm_package.has_apm_dependencies() and not apm_package.get_dev_apm_dependencies():
        _rich_success("No APM dependencies declared in apm.yml -- nothing to update.")
        return

    logger = InstallLogger(verbose=verbose, dry_run=dry_run, partial=False)

    plan_state: dict[str, UpdatePlan | bool] = {"plan": None, "proceeded": False}

    def _plan_callback(plan: UpdatePlan) -> bool:
        """Render plan, prompt, and decide whether to proceed."""
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

    try:
        result = _install_apm_dependencies(
            apm_package,
            update_refs=True,
            verbose=verbose,
            scope=InstallScope.PROJECT,
            logger=logger,
            plan_callback=_plan_callback,
        )
    except FrozenInstallError as e:
        _rich_error(str(e))
        for reason in e.reasons:
            _rich_echo(reason)
        sys.exit(1)
    except AuthenticationError as e:
        _rich_error(str(e))
        if e.diagnostic_context:
            _rich_echo(e.diagnostic_context)
        sys.exit(1)
    except (DirectDependencyError, PolicyViolationError) as e:
        _rich_error(str(e))
        sys.exit(1)
    except click.UsageError:
        raise
    except Exception as e:
        _rich_error(f"Error updating dependencies: {e}")
        if not verbose:
            _rich_info("Run with --verbose for detailed diagnostics.")
        sys.exit(1)

    plan = plan_state.get("plan")
    if plan is None or not isinstance(plan, UpdatePlan):
        return

    if plan_state.get("proceeded"):
        installed = getattr(result, "installed_count", 0)
        if installed:
            _rich_success(f"Updated {installed} APM dependencies.")
        else:
            _rich_success("Update applied.")


__all__ = ["update"]
