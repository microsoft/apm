"""Marketplace output profiles.

This mirrors ``integration.targets`` for marketplace artifact generation:
``outputs`` selects named profiles, and each profile owns the artifact's
default path, config namespace, mapper, and required package metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ENV_VAR_PATTERN = re.compile(r"^APM_MARKETPLACE_[A-Z0-9_]+_PATH$")
_RESERVED_NAMES = frozenset({"all", "none"})
_INVALID_NAME_CHARS = frozenset("=,/ \t")


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

    path_env_var: str
    """Environment variable that overrides the output path for this profile.

    Declared for schema validation at registration time. Env-var consumption
    is NOT yet implemented — planned for v0.15. The field is validated against
    ``_ENV_VAR_PATTERN`` to prevent collisions with sensitive variables.
    """

    required_package_fields: tuple[str, ...] = ()
    """PackageEntry fields required when this output is selected."""

    supports_cli_output_override: bool = False
    """Whether ``apm pack --marketplace-output`` can override this output path."""


def _validate_profile(profile: MarketplaceOutputProfile) -> None:
    """Validate a profile at registration time.

    Guards against reserved sentinel names, CLI-unfriendly characters,
    and env-var names that could collide with sensitive variables.
    """
    if profile.name in _RESERVED_NAMES:
        raise ValueError(f"Profile name {profile.name!r} is reserved as a --marketplace sentinel.")
    if any(c in _INVALID_NAME_CHARS for c in profile.name) or profile.name.startswith("-"):
        raise ValueError(f"Profile name {profile.name!r} contains a CLI-reserved character.")
    if not _ENV_VAR_PATTERN.fullmatch(profile.path_env_var):
        raise ValueError(
            f"Profile {profile.name!r} declares path_env_var "
            f"{profile.path_env_var!r}; expected "
            f"APM_MARKETPLACE_<NAME>_PATH."
        )


DEFAULT_MARKETPLACE_OUTPUT = MarketplaceOutputProfile(
    name="claude",
    config_attr="claude",
    default_output=".claude-plugin/marketplace.json",
    mapper="claude",
    path_env_var="APM_MARKETPLACE_CLAUDE_PATH",
    supports_cli_output_override=True,
)

CODEX_MARKETPLACE_OUTPUT = MarketplaceOutputProfile(
    name="codex",
    config_attr="codex",
    default_output=".agents/plugins/marketplace.json",
    mapper="codex",
    path_env_var="APM_MARKETPLACE_CODEX_PATH",
    required_package_fields=("category",),
)

MARKETPLACE_OUTPUTS: dict[str, MarketplaceOutputProfile] = {
    profile.name: profile
    for profile in (
        DEFAULT_MARKETPLACE_OUTPUT,
        CODEX_MARKETPLACE_OUTPUT,
    )
}

# Validate all registered profiles at module import.
for _profile in MARKETPLACE_OUTPUTS.values():
    _validate_profile(_profile)


def known_output_names() -> frozenset[str]:
    """Return the supported marketplace output names."""
    return frozenset(MARKETPLACE_OUTPUTS)
