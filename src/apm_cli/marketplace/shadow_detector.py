"""Cross-marketplace shadow detection for plugin name squatting.

When a user installs ``my-plugin@acme``, shadow detection checks whether
the same plugin name appears in *other* registered marketplaces.  A match
is not necessarily malicious, but it warrants a warning so the user can
verify they are installing from the intended source.

This module is advisory-only -- errors are logged at DEBUG level and
never propagate to the caller.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional  # noqa: F401, UP035

from .client import fetch_or_cache
from .registry import get_registered_marketplaces

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowMatch:
    """A plugin name found in a secondary marketplace."""

    marketplace_name: str
    plugin_name: str


def detect_shadows(
    plugin_name: str,
    primary_marketplace: str,
    *,
    auth_resolver: object | None = None,
) -> list[ShadowMatch]:
    """Check registered marketplaces for duplicate plugin names.

    Iterates over every registered marketplace *except*
    ``primary_marketplace`` and returns a :class:`ShadowMatch` for each
    one that contains a plugin with the same name (case-insensitive).

    Uses :func:`fetch_or_cache` so cached manifests are reused and no
    extra network round-trips are needed in the common case.

    Args:
        plugin_name: The plugin name to search for.
        primary_marketplace: Marketplace the user explicitly selected
            (excluded from the scan).
        auth_resolver: Optional ``AuthResolver`` forwarded to
            :func:`fetch_or_cache`.

    Returns:
        A list of :class:`ShadowMatch` instances (may be empty).
    """
    shadows: list[ShadowMatch] = []
    for source in get_registered_marketplaces():
        if source.name.lower() == primary_marketplace.lower():
            continue
        try:
            manifest = fetch_or_cache(source, auth_resolver=auth_resolver)
            match = manifest.find_plugin(plugin_name)
            if match is not None:
                shadows.append(
                    ShadowMatch(
                        marketplace_name=source.name,
                        plugin_name=match.name,
                    )
                )
        except Exception as exc:
            logger.debug(
                "Shadow check failed for marketplace '%s': %s",
                source.name,
                exc,
            )
    return shadows
