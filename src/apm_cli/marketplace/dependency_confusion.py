"""Admission gate for the #1326 marketplace dependency-confusion check.

:func:`admit_marketplace_resolution` is the single owner of the decision. Every
install path that turns a marketplace entry into a dependency -- ``apm install
NAME@MARKETPLACE``, a marketplace entry in ``apm.yml``, a transitive
marketplace dependency, and ``apm update`` -- calls it right after
:func:`~apm_cli.marketplace.resolver.resolve_marketplace_plugin` and before any
outbound request to the resolved package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .resolver import CrossRepoMisconfigRisk, MarketplacePluginResolution


class DependencyConfusionError(ValueError):
    """A marketplace entry resolved to an ambiguous bare cross-repo source."""

    def __init__(self, risk: CrossRepoMisconfigRisk) -> None:
        self.risk = risk
        super().__init__(
            "\n".join(
                [
                    f"refused (dependency-confusion risk #1326): bare"
                    f" `repo: {risk.bare_repo_field}` on enterprise"
                    f" marketplace '{risk.marketplace_host}' is ambiguous."
                    f" Host-qualify the plugin `repo` field in"
                    f" marketplace.json to one of:",
                    f"  - '{risk.suggested_qualified_repo}' (enterprise dep on this marketplace)",
                    f"  - 'github.com/{risk.bare_repo_field}' (declared cross-host dep on public github.com)",
                ]
            )
        )


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
