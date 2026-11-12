"""Install presentation layer (dry-run preview, final result rendering)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.integration.cleanup import CleanupResult


def surface_target_cleanup(
    logger: InstallLogger | None, dep_key: str, cleanup: CleanupResult
) -> None:
    """Report a target-contraction cleanup pass at default verbosity.

    Deleting a dropped target's deployed file is a destructive operation in
    the user's tracked workspace, so -- like every other caller of the
    cleanup chokepoint (see ``install/phases/cleanup.py``) -- it must be
    visible without ``--verbose``. Surfaces each user-edit skip as a yellow
    inline and the deletion count as an info line.
    """
    if logger is None:
        return
    for skipped in cleanup.skipped_user_edit:
        logger.cleanup_skipped_user_edit(skipped, dep_key)
    logger.stale_cleanup(dep_key, len(cleanup.deleted))
