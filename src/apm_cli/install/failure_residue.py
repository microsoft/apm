"""Report native target files a failed install leaves on disk.

``InstallTransaction`` restores ``apm.yml`` and ``apm_modules`` staging only.
Native target integrations run outside it, so a failure after integration
leaves those files in place. This module names them truthfully instead of
letting the failure read as a full rollback (#3525). It never deletes,
restores, or records ownership of any file.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


@dataclass(frozen=True)
class FailedInstallResidue:
    """Untracked native target output, split by how a user can remove it."""

    files: tuple[str, ...]
    entries: tuple[str, ...]
    user_scope: bool


def _normalize(path: object) -> str:
    return str(path).replace("\\", "/").rstrip("/")


def _without_aggregate_directories(paths: set[str]) -> set[str]:
    """Drop directory claims whose members are claimed individually.

    Skills record their directory and every member. Only the members were
    written by APM; the directory may also hold unrelated user files.
    """
    ancestors: set[str] = set()
    for path in paths:
        parts = path.split("/")
        ancestors.update("/".join(parts[:index]) for index in range(1, len(parts)))
    return paths - ancestors


def _lockfile_recorded_paths(ctx: InstallContext) -> set[str]:
    """Return the deployed claims the on-disk lockfile records right now."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lockfile = None
    # An unreadable lockfile records nothing the next run can rely on.
    with contextlib.suppress(Exception):
        lockfile = LockFile.read(get_lockfile_path(ctx.apm_dir))
    if lockfile is None:
        return set()
    return {
        _normalize(path) for path in DeploymentLedgerCodec.legacy_deployed_file_claims(lockfile)
    }


def unrecorded_deployed_paths(ctx: InstallContext) -> list[str]:
    """Return deployed claims from this attempt that no lockfile records.

    These are the outputs a later install would treat as not managed by APM.
    """
    deployed = {_normalize(path) for path in ctx.local_deployed_files}
    for paths in ctx.package_deployed_files.values():
        deployed.update(_normalize(path) for path in paths)
    deployed.discard("")
    if not deployed:
        return []
    deployed = _without_aggregate_directories(deployed)
    return sorted(deployed - _lockfile_recorded_paths(ctx))


def _decode_locator(ctx: InstallContext, locator: str) -> str | None:
    """Resolve a file-backed target locator (for example ``cowork://``)."""
    for target in ctx.targets or ():
        root = getattr(target, "managed_deploy_root", None)
        schemes = getattr(target, "lockfile_uri_schemes", ()) or ()
        if root is None or not any(locator.startswith(scheme) for scheme in schemes):
            continue
        with contextlib.suppress(Exception):
            decoded = target.decode_external_locator(locator, root)
            if decoded is not None:
                return str(decoded)
    return None


def failed_install_residue(ctx: InstallContext) -> FailedInstallResidue:
    """Classify this attempt's untracked native target output for display."""
    from apm_cli.core.scope import InstallScope

    user_scope = ctx.scope is InstallScope.USER
    files: list[str] = []
    entries: list[str] = []
    for claim in unrecorded_deployed_paths(ctx):
        if "://" not in claim:
            files.append(f"~/{claim}" if user_scope else claim)
            continue
        decoded = _decode_locator(ctx, claim)
        if decoded is None:
            entries.append(claim)
        else:
            files.append(decoded)
    return FailedInstallResidue(tuple(files), tuple(entries), user_scope)


def report_failed_install_residue(ctx: InstallContext) -> None:
    """Name the native target output a failed install attempt left behind.

    Runs while the original failure propagates, so it never raises: a
    rendering error must not replace the install error.
    """
    if ctx.logger is None or ctx.dry_run or ctx.lockfile_only:
        return
    with contextlib.suppress(Exception):
        residue = failed_install_residue(ctx)
        if residue.files or residue.entries:
            ctx.logger.failed_install_residue(residue)
