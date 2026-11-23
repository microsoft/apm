"""Admission gate for the #1326 marketplace dependency-confusion check.

:func:`admit_marketplace_resolution` is the single owner of the decision. Every
install path that turns a marketplace entry into a dependency -- ``apm install
NAME@MARKETPLACE``, a marketplace entry in ``apm.yml``, a transitive
marketplace dependency, and ``apm update`` -- calls it right after
:func:`~apm_cli.marketplace.resolver.resolve_marketplace_plugin` and before any
request to the resolved package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import DependencyConfusionError

if TYPE_CHECKING:
    from .resolver import MarketplacePluginResolution

__all__ = ["DependencyConfusionError", "admit_marketplace_resolution"]


def admit_marketplace_resolution(resolution: MarketplacePluginResolution) -> None:
    """Refuse a resolution that carries the #1326 cross-repo misconfig sentinel.

    Raises:
        DependencyConfusionError: The plugin's bare ``repo:`` on a ``*.ghe.com``
            marketplace would fall back to public ``github.com``. Host-qualifying
            the field in marketplace.json is the supported escape hatch.
    """
    risk = resolution.cross_repo_misconfig_risk
    if risk is not None:
        raise DependencyConfusionError(risk)
