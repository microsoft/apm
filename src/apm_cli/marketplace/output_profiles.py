"""Marketplace output profiles.

This mirrors ``integration.targets`` for marketplace artifact generation:
``outputs`` selects named profiles, and each profile owns the artifact's
default path, config namespace, mapper, and required package metadata.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..utils.path_security import ensure_path_within

if TYPE_CHECKING:
    from .yml_schema import MarketplaceConfig

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


def resolve_effective_output_path(
    config: MarketplaceConfig,
    profile: MarketplaceOutputProfile,
    project_root: Path,
    output_overrides: Mapping[str, str | Path] | None = None,
) -> Path:
    """Resolve a profile output path with pack's documented precedence."""
    if output_overrides and profile.name in output_overrides:
        configured_path = output_overrides[profile.name]
    else:
        explicit_spec = next(
            (
                spec
                for spec in config.output_specs
                if spec.name == profile.name and spec.path_explicit
            ),
            None,
        )
        if explicit_spec is not None:
            configured_path = explicit_spec.path
        else:
            profile_config = getattr(config, profile.config_attr, None)
            configured_path = getattr(profile_config, "output", profile.default_output)

    output_path = Path(configured_path)
    if not output_path.is_absolute():
        output_path = project_root / output_path
    ensure_path_within(output_path, project_root)
    return output_path
