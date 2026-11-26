"""Leaf helpers shared by install package-reference resolution."""

from ..deps.lockfile import LockFile


def _reject(invalid_outcomes, logger, package, reason) -> None:
    """Record *package* as invalid for *reason* and report it."""
    invalid_outcomes.append((package, reason))
    if logger:
        logger.validation_fail(package, reason)


def _scoped_lockfile(scope):
    """Read the lockfile the scope's marketplace entries were locked in (#3959)."""
    from ..core.scope import InstallScope, get_apm_dir
    from ..deps.lockfile import resolve_lockfile_path_for_read

    # Read-only: a legacy apm.lock not yet migrated still carries the origin.
    apm_dir = get_apm_dir(scope or InstallScope.PROJECT)
    return LockFile.read(resolve_lockfile_path_for_read(apm_dir, read_only=True))
