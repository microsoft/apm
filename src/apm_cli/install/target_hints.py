"""User-facing hints for gated install targets."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from apm_cli.core.target_catalog import get_target_capability


class _TargetLike(Protocol):
    """Minimal resolved-target interface used by hint rendering."""

    name: str


class _LoggerLike(Protocol):
    """Minimal logger interface used by hint rendering."""

    def progress(self, message: str, *, symbol: str) -> None:
        """Render an informational progress message."""


def emit_disabled_experimental_target_hint(
    requested_target: str,
    resolved_targets: Iterable[_TargetLike],
    logger: _LoggerLike | None,
) -> bool:
    """Emit the canonical enable hint when an explicit target is flag-gated."""
    if requested_target == "all":
        return False

    try:
        capability = get_target_capability(requested_target)
    except KeyError:
        return False
    if capability.experimental_flag is None:
        return False
    if any(target.name == capability.name for target in resolved_targets):
        return False

    from apm_cli.core.experimental import is_enabled

    if is_enabled(capability.experimental_flag):
        return False
    if logger is not None:
        logger.progress(
            f"The '{capability.name}' target requires an experimental flag. "
            f"Run: apm experimental enable "
            f"{capability.experimental_flag.replace('_', '-')}",
            symbol="info",
        )
    return True
