"""Explicit compatibility facade for the historical Claude plugin exporter."""

from pathlib import Path

from .packer import PackResult
from .plugin_exporter import export_plugin_bundle


def export_claude_plugin_bundle(
    project_root: Path,
    output_dir: Path,
    target: str | None = None,
    archive: bool = False,
    archive_format: str = "zip",
    dry_run: bool = False,
    force: bool = False,
    logger=None,
) -> PackResult:
    """Export the historical Claude plugin layout without changing its behavior."""
    return export_plugin_bundle(
        project_root=project_root,
        output_dir=output_dir,
        target=target,
        archive=archive,
        archive_format=archive_format,
        dry_run=dry_run,
        force=force,
        logger=logger,
    )


__all__ = ["export_claude_plugin_bundle"]
