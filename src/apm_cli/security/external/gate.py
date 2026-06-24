"""Experimental feature gate for external SARIF-native scanner ingestion.

Mirrors :mod:`apm_cli.deps.registry.feature_gate`: a thin module exposing an
``is_*_enabled`` query and a ``require_*_enabled`` guard that raises a
consistent, actionable error when the ``external_scanners`` experimental flag
is off.  Fail-closed: the flag defaults to ``False``.
"""

from __future__ import annotations

FLAG_NAME = "external_scanners"
DISPLAY_NAME = "external-scanners"
ENABLE_COMMAND = f"apm experimental enable {DISPLAY_NAME}"


class ExternalScannersFeatureDisabledError(ValueError):
    """Raised when external scanner ingestion is used without opt-in."""


def is_external_scanners_enabled() -> bool:
    """Return whether the ``external_scanners`` experimental flag is enabled."""
    from apm_cli.core.experimental import is_enabled

    return is_enabled(FLAG_NAME)


def require_external_scanners_enabled(action: str = "External scanner ingestion") -> None:
    """Raise a consistent error if external scanner ingestion is disabled."""
    if is_external_scanners_enabled():
        return
    raise ExternalScannersFeatureDisabledError(
        f"{action} requires the experimental {DISPLAY_NAME} feature. "
        f"Enable with: {ENABLE_COMMAND}. "
        "Run 'apm experimental list' to see available experimental features."
    )
