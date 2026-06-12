"""``apm lock`` -- resolve dependencies and write ``apm.lock.yaml`` without deploying files.

Mirrors the lockfile-generation ergonomics of ``cargo generate-lockfile``
and ``pnpm lock``: run the full resolver and downloader so every commit
SHA is pinned, then write ``apm.lock.yaml`` -- **without** copying any
primitives into agent targets (no ``.github/``, no ``.agents/``, etc.).

Use ``apm lock`` to:

* Bootstrap a lockfile before the first ``apm install`` run in CI.
* Refresh the lockfile after editing ``apm.yml`` without triggering a
  full deployment (useful when you want to review changes before
  applying them).
* Verify that the current ``apm.yml`` resolves cleanly.

What it does
------------
1. Parses ``apm.yml``.
2. Runs the resolve + download phases (network required for fresh deps).
3. Writes ``apm.lock.yaml`` with all pinned SHAs and content hashes.
4. Skips the targets, cleanup, post-deps-local, and audit phases.
   The integrate phase still runs but deploys nothing because the
   target set is empty in lockfile-only mode.

Flags
-----
* ``--verbose``/``-v`` -- show per-dependency resolution details.
* ``--global``/``-g`` -- operate on ``~/.apm/apm.yml`` instead of the
  current project (mirrors ``apm install -g``).
* ``--update`` -- re-resolve refs to their latest SHAs (like
  ``apm install --update``) before writing the lockfile.
* ``--no-policy`` -- skip policy enforcement during resolution.
* ``--target``/``-t`` -- scope policy enforcement to a specific agent
  target during resolution; no files are deployed.
* ``--parallel-downloads`` -- max concurrent package downloads.
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
    PolicyViolationError,
)
from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success
from ._helpers import _find_apm_yml


def _handle_lock_error(e: Exception, verbose: bool) -> None:
    """Render a pipeline error and exit.

    Centralises error rendering for the ``apm lock`` command so the
    except block below stays short and the pattern stays distinct from
    the similar handler in ``apm update``.
    """
    if isinstance(e, AuthenticationError):
        _rich_error(str(e))
        if e.diagnostic_context:
            _rich_echo(e.diagnostic_context)
        _rich_info("Tip: run 'apm doctor' to diagnose auth and connectivity.", symbol="info")
    elif isinstance(e, (DirectDependencyError, PolicyViolationError)):
        _rich_error(str(e))
    else:
        _rich_error(f"Error generating lockfile: {e}")
        if not verbose:
            _rich_info("Run with --verbose for detailed diagnostics.", symbol="info")
    sys.exit(1)


@click.command(
    name="lock",
    help="Resolve dependencies and write apm.lock.yaml without deploying files",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Show per-dependency resolution details",
)
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Operate on ~/.apm/apm.yml instead of the current project",
)
@click.option(
    "--update",
    "update_refs",
    is_flag=True,
    default=False,
    help="Re-resolve refs to their latest SHAs before writing the lockfile",
)
@click.option(
    "--no-policy",
    is_flag=True,
    default=False,
    help="Skip policy enforcement during resolution",
)
@click.option(
    "--target",
    "-t",
    type=TargetParamType(),
    default=None,
    help=(
        "Agent target(s) to scope policy enforcement during resolution "
        "(e.g. claude, copilot, cursor). "
        "No files are deployed regardless of this value."
    ),
)
@click.option(
    "--parallel-downloads",
    type=int,
    default=4,
    show_default=True,
    help="Max concurrent package downloads (0 to disable parallelism)",
)
def lock(
    verbose: bool,
    global_: bool,
    update_refs: bool,
    no_policy: bool,
    target: str | list[str] | None,
    parallel_downloads: int,
) -> None:
    """Resolve dependencies and write apm.lock.yaml without deploying files.

    Examples:
        apm lock                 # Resolve from apm.yml, write apm.lock.yaml
        apm lock --update        # Re-resolve to latest SHAs, write lockfile
        apm lock -g              # Operate on the user-scope (~/.apm/) manifest
        apm lock --verbose       # Show resolution details
    """
    import os

    from apm_cli.core.scope import InstallScope, get_apm_dir
    from apm_cli.models.apm_package import APMPackage

    if global_:
        scope = InstallScope.USER
        manifest_path = get_apm_dir(scope) / "apm.yml"
        if not manifest_path.is_file():
            _rich_error(
                "No apm.yml found in ~/.apm/. Run 'apm install -g <org/repo>' to create one."
            )
            sys.exit(1)
        project_root = manifest_path.parent
    else:
        scope = InstallScope.PROJECT
        manifest_path = _find_apm_yml()
        if manifest_path is None:
            _rich_error(
                "No apm.yml found in this directory or any parent directory. "
                "Run 'apm init' to create one."
            )
            sys.exit(1)
        project_root = manifest_path.parent
        if project_root != Path.cwd().resolve():
            _rich_info(
                f"Using apm.yml at {manifest_path} (project root: {project_root})",
                symbol="info",
            )

    if project_root != Path.cwd().resolve():
        os.chdir(project_root)

    try:
        apm_package = APMPackage.from_apm_yml(Path("apm.yml"))
    except (FileNotFoundError, ValueError) as e:
        _rich_error(f"Failed to parse apm.yml: {e}")
        sys.exit(1)

    logger = InstallLogger(verbose=verbose)

    try:
        from apm_cli.commands.install import _install_apm_dependencies

        _install_apm_dependencies(
            apm_package,
            update_refs=update_refs,
            verbose=verbose,
            scope=scope,
            parallel_downloads=parallel_downloads,
            logger=logger,
            no_policy=no_policy,
            target=target,
            lockfile_only=True,
        )
    except click.UsageError:
        raise
    except Exception as e:
        _handle_lock_error(e, verbose)

    _rich_success("Lockfile written to apm.lock.yaml", symbol="check")


__all__ = ["lock"]
