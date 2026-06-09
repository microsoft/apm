"""Target-detection helpers extracted to keep target_detection.py under 800 lines.

Re-exported from ``target_detection`` so all existing import paths keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .target_detection import ResolvedTargets


def _target_error(message: str, source_path: Path | None) -> str:
    """Format a target validation error, naming the source file when known."""
    if source_path is not None:
        return f"Invalid 'target' in {source_path}: {message}"
    return f"Invalid target: {message}"


def format_provenance(resolved: ResolvedTargets) -> str:
    """Format provenance line for CLI output.

    Returns the message portion (without the [i] prefix, since
    _rich_info adds it).

    # Double-space between target list and metadata is intentional and
    # canonical. Test assertions match this exact spacing. Do not collapse.
    """
    targets_csv = ", ".join(resolved.targets)
    return f"Targets: {targets_csv}  (source: {resolved.source})"
