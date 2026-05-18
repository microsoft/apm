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
* ``--target``/``-t`` -- agent harness(es) to deploy to (e.g.
  ``claude``, ``copilot``, ``cursor``, ``windsurf``, ``codex``,
  ``opencode``, ``gemini``); comma-separated for multiple targets.
  Overrides ``apm.yml targets:`` and auto-detection.

Other ``apm install`` flags are NOT mirrored here on purpose -- the
update command stays focused on the refresh-and-confirm loop.
``apm install --update`` remains the swiss-army-knife escape hatch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..core.command_logger import InstallLogger
from ..core.target_detection import TargetParamType
from ..utils.console import _rich_info, _rich_warning
from .update_helpers import (
    _build_plan_callback,
    _load_update_package,
    _prepare_update_runtime,
    _render_update_result,
    _run_update_install,
    _stdin_is_tty,
)


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
    assume_yes: bool,
    dry_run: bool,
    verbose: bool,
    check_only: bool,
    target: str | list[str] | None,
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
    )


def _run_dep_update(
    *,
    assume_yes: bool,
    dry_run: bool,
    verbose: bool,
    project_root: Path | None = None,
    target: str | list[str] | None = None,
) -> None:
    """Core ``apm update`` flow: resolve, plan, prompt, install."""
    _install_apm_dependencies, InstallScope, APMPackage = _prepare_update_runtime(project_root)
    apm_package = _load_update_package(APMPackage)
    if apm_package is None:
        return

    logger = InstallLogger(verbose=verbose, dry_run=dry_run, partial=False)
    plan_state = {"plan": None, "proceeded": False}
    result = _run_update_install(
        _install_apm_dependencies,
        InstallScope,
        apm_package,
        logger,
        _build_plan_callback(
            plan_state,
            assume_yes=assume_yes,
            dry_run=dry_run,
            verbose=verbose,
        ),
        verbose=verbose,
        target=target,
    )
    _render_update_result(plan_state, result)


__all__ = ["update"]
