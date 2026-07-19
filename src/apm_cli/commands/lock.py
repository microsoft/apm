"""``apm lock`` -- resolve dependencies and write ``apm.lock.yaml`` without deploying or deleting files.

Mirrors the lockfile-generation ergonomics of ``cargo generate-lockfile``
and ``pnpm lock``: run the full resolver and downloader so every commit
SHA is pinned, then write ``apm.lock.yaml`` -- **without** copying or
deleting primitives in agent targets (no ``.github/``, no ``.agents/``,
etc.).

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
from ..export.formats import FORMAT_CYCLONEDX, SUPPORTED_FORMATS
from ..install.errors import (
    AuthenticationError,
    DirectDependencyError,
    PolicyViolationError,
)
from ..utils.console import (
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_success,
)
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


@click.group(
    name="lock",
    invoke_without_command=True,
    help="Resolve dependencies and write apm.lock.yaml without deploying or deleting files",
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
        "No files are deployed or deleted regardless of this value."
    ),
)
@click.option(
    "--parallel-downloads",
    type=int,
    default=4,
    show_default=True,
    help="Max concurrent package downloads (0 to disable parallelism)",
)
@click.pass_context
def lock(
    ctx: click.Context,
    verbose: bool,
    global_: bool,
    update_refs: bool,
    no_policy: bool,
    target: str | list[str] | None,
    parallel_downloads: int,
) -> None:
    """Resolve dependencies and write apm.lock.yaml without deploying or deleting files.

    Run bare to (re)generate the lockfile, or use a subcommand such as
    ``apm lock export`` to derive an artifact from the existing lockfile.

    Examples:
        apm lock                 # Resolve from apm.yml, write apm.lock.yaml
        apm lock --update        # Re-resolve to latest SHAs, write lockfile
        apm lock -g              # Operate on the user-scope (~/.apm/) manifest
        apm lock --verbose       # Show resolution details
        apm lock export --format cyclonedx > sbom.json
    """
    # Subcommands (e.g. ``export``) own their own behavior; only the bare
    # ``apm lock`` invocation runs the resolve-and-write flow.
    if ctx.invoked_subcommand is not None:
        return
    _run_lock(
        verbose=verbose,
        global_=global_,
        update_refs=update_refs,
        no_policy=no_policy,
        target=target,
        parallel_downloads=parallel_downloads,
    )


def _run_lock(
    *,
    verbose: bool,
    global_: bool,
    update_refs: bool,
    no_policy: bool,
    target: str | list[str] | None,
    parallel_downloads: int,
) -> None:
    """Resolve dependencies and write apm.lock.yaml (bare ``apm lock``)."""
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


@lock.command(
    name="export",
    help="Export an SBOM/inventory from the existing lockfile (reads apm.lock.yaml only)",
)
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(list(SUPPORTED_FORMATS), case_sensitive=False),
    default=FORMAT_CYCLONEDX,
    show_default=True,
    help="SBOM output format",
)
@click.option(
    "--output",
    "-o",
    "output",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write the SBOM to a file instead of stdout",
)
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Read the user-scope (~/.apm/) lockfile instead of the current project",
)
@click.option(
    "--timestamp",
    "timestamp",
    default=None,
    help=(
        "Pin the SBOM timestamp (ISO 8601, e.g. 2024-06-01T00:00:00+00:00) for "
        "reproducible output. Defaults to SOURCE_DATE_EPOCH, then the lockfile's "
        "generated_at."
    ),
)
def lock_export(fmt: str, output: str | None, global_: bool, timestamp: str | None) -> None:
    """Serialize the recorded lockfile into a CycloneDX or SPDX inventory.

    This is an INVENTORY export, not a security attestation: it reflects what
    the lockfile recorded (purls, hashes, declared licenses) and never
    re-resolves, re-hashes, or touches the network.
    """
    from apm_cli.core.scope import InstallScope, get_apm_dir
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.export.sbom import export_sbom

    if global_:
        project_root = get_apm_dir(InstallScope.USER)
    else:
        manifest_path = _find_apm_yml()
        project_root = manifest_path.parent if manifest_path else Path.cwd().resolve()

    lockfile_path = get_lockfile_path(project_root)
    if not lockfile_path.is_file():
        _rich_error(f"No lockfile found at {lockfile_path}. Run 'apm lock' to generate one first.")
        sys.exit(1)

    lockfile = LockFile.from_yaml(lockfile_path.read_text(encoding="utf-8"))
    resolved_timestamp = _resolve_export_timestamp(timestamp, lockfile.generated_at)

    document = export_sbom(lockfile, fmt, timestamp=resolved_timestamp)

    if output:
        Path(output).write_text(document, encoding="utf-8")
        _rich_success(f"SBOM written to {output} (format={fmt.lower()})", symbol="check")
    else:
        click.echo(document, nl=False)


def _resolve_export_timestamp(explicit: str | None, lockfile_generated_at: str | None) -> str:
    """Resolve the SBOM timestamp for reproducible output.

    Precedence: explicit ``--timestamp`` > ``SOURCE_DATE_EPOCH`` env >
    the lockfile's ``generated_at`` > a fixed epoch. Pinning keeps export
    byte-deterministic across runs.
    """
    import os
    from datetime import datetime, timezone

    if explicit:
        return explicit
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    if lockfile_generated_at:
        return lockfile_generated_at
    return "1970-01-01T00:00:00+00:00"


__all__ = ["lock"]
