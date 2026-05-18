"""Local-path validation helpers for the install validation pipeline.

Extracted from ``apm_cli.install.validation`` so the filesystem-probe logic
lives in a focused, independently testable module.
``apm_cli.install.validation`` re-exports both public names so all existing
import sites remain valid.
"""

from __future__ import annotations

from pathlib import Path

from apm_cli.utils.console import _rich_echo, _rich_info

__all__ = ["_local_path_failure_reason", "_local_path_no_markers_hint"]


def _emit_package_hints(found, logger=None) -> None:
    if logger:
        logger.progress("  [i] Found installable package(s) inside this directory:")
        for path in found[:5]:
            logger.verbose_detail(f"      apm install {path}")
        if len(found) > 5:
            logger.verbose_detail(f"      ... and {len(found) - 5} more")
        return
    _rich_info("  [i] Found installable package(s) inside this directory:")
    for path in found[:5]:
        _rich_echo(f"      apm install {path}", color="dim")
    if len(found) > 5:
        _rich_echo(f"      ... and {len(found) - 5} more", color="dim")


def _local_path_failure_reason(dep_ref):
    """Return a specific failure reason for local path deps, or None for remote."""
    if not (dep_ref.is_local and dep_ref.local_path):
        return None
    local = Path(dep_ref.local_path).expanduser()
    if not local.is_absolute():
        local = Path.cwd() / local
    local = local.resolve()
    if not local.exists():
        return "path does not exist"
    if not local.is_dir():
        return "path is not a directory"
    # Directory exists but has no package markers
    return "no apm.yml, SKILL.md, or plugin.json found"


def _local_path_no_markers_hint(local_dir, logger=None):
    """Scan two levels for sub-packages and print a hint if any are found."""
    from apm_cli.utils.helpers import find_plugin_json

    markers = ("apm.yml", "SKILL.md")
    found = []
    for child in sorted(local_dir.iterdir()):
        if not child.is_dir():
            continue
        if any((child / m).exists() for m in markers) or find_plugin_json(child) is not None:
            found.append(child)
        # Also check one more level (e.g. skills/<name>/)
        for grandchild in sorted(child.iterdir()) if child.is_dir() else []:
            if not grandchild.is_dir():
                continue
            if (
                any((grandchild / m).exists() for m in markers)
                or find_plugin_json(grandchild) is not None
            ):
                found.append(grandchild)

    if found:
        _emit_package_hints(found, logger)
