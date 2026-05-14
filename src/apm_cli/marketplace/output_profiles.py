"""Marketplace output profiles.

This mirrors ``integration.targets`` for marketplace artifact generation:
``outputs`` selects named profiles, and each profile owns the artifact's
default path, config namespace, mapper, and required package metadata.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketplaceOutputProfile:
    """Capabilities and layout of a marketplace output artifact."""

    name: str
    """Short unique identifier (``"claude"``, ``"codex"``)."""

    config_attr: str
    """Attribute on ``MarketplaceConfig`` containing output-specific config."""

    default_output: str
    """Default output path relative to the project root."""

    mapper: str
    """Mapper identifier used by ``MarketplaceBuilder`` to build the JSON."""

    required_package_fields: tuple[str, ...] = ()
    """PackageEntry fields required when this output is selected."""

    supports_cli_output_override: bool = False
    """Whether ``apm pack --marketplace-output`` can override this output path."""


DEFAULT_MARKETPLACE_OUTPUT = MarketplaceOutputProfile(
    name="claude",
    config_attr="claude",
    default_output=".claude-plugin/marketplace.json",
    mapper="claude",
    supports_cli_output_override=True,
)

CODEX_MARKETPLACE_OUTPUT = MarketplaceOutputProfile(
    name="codex",
    config_attr="codex",
    default_output=".agents/plugins/marketplace.json",
    mapper="codex",
    required_package_fields=("category",),
)

MARKETPLACE_OUTPUTS: dict[str, MarketplaceOutputProfile] = {
    profile.name: profile
    for profile in (
        DEFAULT_MARKETPLACE_OUTPUT,
        CODEX_MARKETPLACE_OUTPUT,
    )
}


def known_output_names() -> frozenset[str]:
    """Return the supported marketplace output names."""
    return frozenset(MARKETPLACE_OUTPUTS)
