"""Name -> adapter resolution for external SARIF-native scanners.

Keeps the ``--external <name>`` CLI option decoupled from concrete adapter
classes.  Adding a new vendor is a one-line registry entry plus its adapter
module -- no CLI changes.
"""

from __future__ import annotations

from pathlib import Path

from .base import ExternalScanner

#: Stable set of supported external scanner names (for help text / validation).
SUPPORTED_SCANNERS: tuple[str, ...] = ("skillspector", "sarif")


def resolve_scanner(name: str, *, sarif_file: str | Path | None = None) -> ExternalScanner:
    """Return an adapter instance for *name*.

    Args:
        name: A supported scanner name (see :data:`SUPPORTED_SCANNERS`).
        sarif_file: Path to a pre-generated SARIF file; required by the
            generic ``"sarif"`` adapter, ignored by others.

    Raises:
        ValueError: If *name* is not a supported scanner.
    """
    key = name.strip().lower()
    if key == "skillspector":
        from .skillspector import SkillSpectorAdapter

        return SkillSpectorAdapter()
    if key == "sarif":
        from .generic_sarif import GenericSarifAdapter

        return GenericSarifAdapter(sarif_file=sarif_file)

    raise ValueError(
        f"Unknown external scanner: {name!r}. Supported: {', '.join(SUPPORTED_SCANNERS)}."
    )
