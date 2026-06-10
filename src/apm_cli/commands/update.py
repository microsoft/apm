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
* ``--global``/``-g`` -- refresh user-scope dependencies under
  ``~/.apm/`` instead of the current project (mirrors
  ``apm install -g``).
* ``[PACKAGES]...`` -- positional names to refresh only those
  dependencies; omit to refresh everything.
* ``--force`` -- overwrite locally-authored files on collision.
* ``--parallel-downloads`` -- max concurrent package downloads
  (0 disables parallelism).
* ``--target``/``-t`` -- agent harness(es) to deploy to (e.g.
  ``claude``, ``copilot``, ``cursor``, ``windsurf``, ``codex``,
  ``opencode``, ``gemini``); comma-separated for multiple targets.
  Overrides ``apm.yml targets:`` and auto-detection.

These flags make ``apm update`` a strict superset of the deprecated
``apm deps update`` (issue #1525). ``apm install --update`` remains the
swiss-army-knife escape hatch for the rest of the install surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..core.command_logger import InstallLogger
from ..core.target_detection import TargetParamType
from ..install.errors import (
    AuthenticationError,
    DirectDependencyError,
    FrozenInstallError,
    PolicyViolationError,
)
from ..install.plan import UpdatePlan, render_plan_text
from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success, _rich_warning
from ._helpers import UnknownPackageError, _find_apm_yml, resolve_requested_packages


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
@click.argument("packages", nargs=-1)
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
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Refresh user-scope dependencies (~/.apm/) instead of the current project",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite locally-authored files on collision",
)
@click.option(
    "--parallel-downloads",
    type=int,
    default=4,
    show_default=True,
    help="Max concurrent package downloads (0 to disable parallelism)",
)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    default=False,
    help="(Deprecated) Forwarded to 'apm self-update --check' when run outside an apm.yml project; rejected inside a project.",
    hidden=True,
)
@click.option(
    "--target",
    "-t",
    type=TargetParamType(),
    default=None,
    help=(
        "Agent target(s) to update for "
        "(e.g. claude, copilot, cursor, windsurf, codex, opencode, gemini). "
        "Comma-separated for multiple: --target claude,cursor. "
        "Highest-priority entry in the resolution chain "
        "(--target > apm.yml targets: > auto-detect)."
    ),
)
@click.pass_context
def update(
    ctx: click.Context,
    packages: tuple[str, ...],
    assume_yes: bool,
    dry_run: bool,
    verbose: bool,
    global_: bool,
    force: bool,
    parallel_downloads: int,
    check_only: bool,
    target: str | list[str] | None,
) -> None:
    """Refresh APM dependencies to the latest matching refs.

    Examples:
        apm update                      # Resolve, show plan, prompt, then install
        apm update --dry-run            # Show plan only, do not change anything
        apm update --yes                # Skip the prompt (CI-safe)
        apm update org/pkg-a org/pkg-b  # Refresh only the named packages
        apm update -g                   # Refresh user-scope deps (~/.apm/)
    """
    from apm_cli.core.scope import InstallScope, get_apm_dir

    if global_:
        # User scope: operate on ~/.apm/apm.yml. The cwd manifest walk and
        # the self-update back-compat shim apply only to project scope.
        scope = InstallScope.USER
        manifest_path = get_apm_dir(scope) / "apm.yml"
        if not manifest_path.is_file():
            _rich_error(
                "No apm.yml found in ~/.apm/. Run 'apm install -g <org/repo>' to create one."
            )
            sys.exit(1)
        if check_only:
            _rich_warning(
                "--check applies only to the self-update shim and is ignored with --global.",
                symbol="warning",
            )
        project_root = manifest_path.parent
    else:
        scope = InstallScope.PROJECT
        manifest_path = _find_apm_yml()
        if manifest_path is None:
            # Back-compat shim (one-release): when run outside a project,
            # forward to the renamed self-updater so existing users keep
            # working while we publicise ``apm self-update``.  Removed in
            # the release after this one.
            from apm_cli.commands.self_update import self_update as _self_update_cmd

            if target is not None:
                _rich_warning(
                    "--target is ignored when forwarding to 'apm self-update' "
                    "(no apm.yml found). Use 'apm self-update' directly.",
                    symbol="warning",
                )
            _rich_warning(
                "'apm update' refreshes APM dependencies. To update the CLI binary, "
                "use 'apm self-update'. Forwarding for back-compat (deprecated).",
                symbol="warning",
            )
            ctx.invoke(_self_update_cmd, check=check_only)
            return

        if check_only:
            from apm_cli.commands.self_update import self_update as _self_update_cmd

            if target is not None:
                _rich_warning(
                    "--target is ignored when forwarding to 'apm self-update --check'. "
                    "Use 'apm update --dry-run' to preview dependency changes.",
                    symbol="warning",
                )
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
        target=target,
        scope=scope,
        packages=packages,
        force=force,
        parallel_downloads=parallel_downloads,
    )


def _run_dep_update(
    *,
    assume_yes: bool,
    dry_run: bool,
    verbose: bool,
    project_root: Path | None = None,
    target: str | list[str] | None = None,
    scope=None,
    packages: tuple[str, ...] = (),
    force: bool = False,
    parallel_downloads: int = 4,
) -> None:
    """Core ``apm update`` flow: resolve, plan, prompt, install.

    When ``project_root`` is provided, the working directory is
    switched to it before running so install pipeline paths
    (``apm.yml``, ``apm.lock.yaml``, deployed primitives) resolve
    against the discovered project root, not the caller's cwd.

    ``scope`` selects project vs user deployment (defaults to project).
    ``packages`` narrows the refresh to the named dependencies; ``force``
    and ``parallel_downloads`` mirror the install-pipeline flags.
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

    if scope is None:
        scope = InstallScope.PROJECT

    try:
        apm_package = APMPackage.from_apm_yml(Path("apm.yml"))
    except (FileNotFoundError, ValueError) as e:
        _rich_error(f"Failed to parse apm.yml: {e}")
        sys.exit(1)

    if not apm_package.has_apm_dependencies() and not apm_package.get_dev_apm_dependencies():
        _rich_success("No APM dependencies declared in apm.yml -- nothing to update.")
        return

    # Map any positional [PACKAGES] to canonical dependency keys for the
    # engine's only_packages filter; None means "refresh everything".
    try:
        only_packages = resolve_requested_packages(
            packages,
            apm_package.get_apm_dependencies() + apm_package.get_dev_apm_dependencies(),
        )
    except UnknownPackageError as e:
        _rich_error(f"Package '{e.token}' not found in apm.yml")
        _rich_info(f"Available: {', '.join(e.available)}", symbol="info")
        sys.exit(1)

    logger = InstallLogger(verbose=verbose, dry_run=dry_run, partial=bool(packages))

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
            scope=scope,
            only_packages=only_packages,
            force=force,
            parallel_downloads=parallel_downloads,
            logger=logger,
            plan_callback=_plan_callback,
            target=target,
        )
    except FrozenInstallError as e:
        _rich_error(str(e))
        for reason in e.reasons:
            _rich_echo(reason)
        _rich_info(
            "Tip: run 'apm outdated' to see what changed, then 'apm update'.",
            symbol="info",
        )
        sys.exit(1)
    except AuthenticationError as e:
        _rich_error(str(e))
        if e.diagnostic_context:
            _rich_echo(e.diagnostic_context)
        _rich_info("Tip: run 'apm doctor' to diagnose auth and connectivity.", symbol="info")
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
