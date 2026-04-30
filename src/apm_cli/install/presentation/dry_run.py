"""Dry-run presentation for ``apm install --dry-run``.

Extracted from ``commands/install.py`` (P2.S5) -- faithful copy of the
original block that lived at lines 525-581.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Optional  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

    from apm_cli.commands.install import InstallLogger


def render_and_exit(
    *,
    logger: InstallLogger,
    should_install_apm: bool,
    apm_deps: Sequence[Any],
    mcp_deps: Sequence[Any],
    dev_apm_deps: Sequence[Any],
    should_install_mcp: bool,
    update: bool,
    only_packages: Sequence[str] | None = None,
    apm_dir: Path,
) -> None:
    """Render the dry-run preview to the user.

    The caller is responsible for ``return``-ing after this function
    completes -- this function does NOT exit or return early on its own.
    """
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.drift import detect_orphans

    logger.progress("Dry run mode - showing what would be installed:")

    if should_install_apm and apm_deps:
        logger.progress(f"APM dependencies ({len(apm_deps)}):")
        for dep in apm_deps:
            action = "update" if update else "install"
            logger.progress(f"  - {dep.repo_url}#{dep.reference or 'main'} -> {action}")

    if should_install_mcp and mcp_deps:
        logger.progress(f"MCP dependencies ({len(mcp_deps)}):")
        for dep in mcp_deps:
            logger.progress(f"  - {dep}")

    if not apm_deps and not dev_apm_deps and not mcp_deps:
        logger.progress("No dependencies found in apm.yml")

    # Orphan preview: lockfile + manifest difference -- no integration
    # required, accurate to compute.
    try:
        _dryrun_lock = LockFile.read(get_lockfile_path(apm_dir))
    except Exception:
        _dryrun_lock = None
    if _dryrun_lock:
        # builtins.set used for safety -- matches the original extraction
        # site where ``set`` may be shadowed in the enclosing scope.
        _intended_keys = builtins.set()
        for _dep in (apm_deps or []) + (dev_apm_deps or []):
            try:  # noqa: SIM105
                _intended_keys.add(_dep.get_unique_key())
            except Exception:
                pass
        _orphan_preview = detect_orphans(
            _dryrun_lock,
            _intended_keys,
            only_packages=only_packages,
        )
        if _orphan_preview:
            logger.progress(
                f"Files that would be removed (packages no longer in apm.yml): "
                f"{len(_orphan_preview)}"
            )
            for _orphan in sorted(_orphan_preview)[:10]:
                logger.progress(f"  - {_orphan}")
            if len(_orphan_preview) > 10:
                logger.progress(f"  ... and {len(_orphan_preview) - 10} more")

    if apm_deps or dev_apm_deps:
        logger.dry_run_notice(
            "Per-package stale-file cleanup (renames within a package) is "
            "not previewed -- it requires running integration. Run without "
            "--dry-run to apply."
        )

    logger.success("Dry run complete - no changes made")
