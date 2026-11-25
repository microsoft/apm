"""Settle a written lockfile on one package content-hash stream (#4000).

Hash framing hardening: lockfile version 4 records package ``content_hash``
values in the framed stream, versions 1-3 in the legacy stream. A writable
install promotes a version 1-3 lockfile once, all or nothing, and only from
verified bytes. ``--frozen`` and dry runs keep the stream they read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile

PROMOTED_MESSAGE = (
    'Upgraded apm.lock.yaml to lockfile_version "4" (hash framing hardening); '
    "every package content_hash changes once. Commit it once CI runs APM 0.37 or later."
)


def promote_content_hashes(ctx: Any, lockfile: LockFile) -> bool:
    """Put every package row of *lockfile* on one content-hash stream.

    Returns True when a version 1-3 lockfile moved to the framed stream, so
    the caller can explain the one-time change once the write succeeds.

    Rows recorded this run carry a framed digest from the same read. When the
    existing lockfile is already framed, its carried rows are framed too.
    Other carried rows are re-hashed from ``apm_modules`` only after cheap
    checks pass for every one of them and their legacy hash verifies.
    """
    from apm_cli.deps.carried_license import verified_framed_digest
    from apm_cli.deps.lockfile import _SELF_KEY, records_framed_content_hashes
    from apm_cli.install.service import InstallService

    recorded_framed = records_framed_content_hashes(getattr(ctx, "existing_lockfile", None))
    withheld = InstallService.frozen_disposition(ctx).withhold_lockfile_writes
    if withheld or getattr(ctx, "dry_run", False):
        lockfile.set_content_hash_stream(framed=recorded_framed)
        return False
    recorded = getattr(ctx, "package_hashes", {})
    recorded_framed_digests = getattr(ctx, "package_framed_hashes", {})
    promoted: dict[str, str] = {}
    pending: dict[str, LockedDependency] = {}
    for dep_key, dep in lockfile.dependencies.items():
        if dep_key == _SELF_KEY or not dep.content_hash:
            continue
        if recorded.get(dep_key) == dep.content_hash and dep_key in recorded_framed_digests:
            promoted[dep_key] = recorded_framed_digests[dep_key]
        elif recorded_framed and dep.content_hash_framed:
            promoted[dep_key] = dep.content_hash
        else:
            pending[dep_key] = dep
    paths = {key: _carried_install_path(ctx, dep) for key, dep in pending.items()}
    blocked = next((key for key, path in paths.items() if path is None), None)
    for dep_key, dep in pending.items() if blocked is None else ():
        framed = verified_framed_digest(paths[dep_key], dep.content_hash, dep.carried_licenses)
        if framed is None:
            blocked = dep_key
            break
        promoted[dep_key] = framed
    if blocked is not None:
        _keep_stream(ctx, lockfile, blocked, recorded_framed)
        return False
    for dep_key, framed in promoted.items():
        lockfile.dependencies[dep_key].content_hash = framed
    lockfile.set_content_hash_stream(framed=True)
    return not recorded_framed and bool(promoted)


def _carried_install_path(ctx: Any, dep: LockedDependency):
    """``apm_modules`` directory of a carried remote row, else None (cheap checks only)."""
    modules = getattr(ctx, "apm_modules_dir", None)
    if dep.source == "local" or modules is None:
        return None
    try:
        path = dep.to_dependency_ref().get_install_path(modules)
    except (OSError, ValueError):
        return None
    return path if path.is_dir() else None


def _keep_stream(ctx: Any, lockfile: LockFile, dep_key: str, recorded_framed: bool) -> None:
    from apm_cli.core.remediation import CONTENT_HASH_UNVERIFIED

    hint = CONTENT_HASH_UNVERIFIED.bind(package=dep_key).clause()
    if recorded_framed:  # this run's rows are framed: never mislabel them
        raise RuntimeError(f"Cannot verify the locked content of {dep_key}; {hint}")
    lockfile.set_content_hash_stream(framed=False)
    logger = getattr(ctx, "logger", None)
    if logger is not None:
        logger.verbose_detail(
            f"apm.lock.yaml keeps its lockfile_version: {dep_key} has no verified bytes; {hint}"
        )
