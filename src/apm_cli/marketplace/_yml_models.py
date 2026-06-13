"""Dataclasses for marketplace authoring configuration.

Leaf module -- contains only frozen dataclasses.  No imports from
``yml_schema`` or ``_yml_parsers`` (cycle-safe).  All public symbols
are re-exported by ``yml_schema`` so existing import paths continue to
work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping  # noqa: UP035

from .output_profiles import MARKETPLACE_OUTPUTS

__all__ = [
    "MarketplaceBuild",
    "MarketplaceClaudeConfig",
    "MarketplaceCodexConfig",
    "MarketplaceConfig",
    "MarketplaceOutputSpec",
    "MarketplaceOwner",
    "MarketplaceVersioning",
    "PackageEntry",
]


@dataclass(frozen=True)
class MarketplaceOwner:
    """Owner block of ``marketplace.yml``."""

    name: str
    email: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class MarketplaceBuild:
    """APM-only build configuration block."""

    tag_pattern: str = "v{version}"


@dataclass(frozen=True)
class MarketplaceVersioning:
    """Release-time versioning strategy for the marketplace.

    Controls how ``apm pack --check-versions`` verifies per-package
    version alignment across local-path packages:

    * ``lockstep`` (default) -- every local package's top-level
      ``version`` must equal the marketplace's top-level ``version``.
    * ``tag_pattern`` -- each rendered tag must be unique across all
      local packages; missing ``version`` still fails.
    * ``per_package`` -- only requires that each local package declare
      a ``version``; equality is not enforced.
    """

    strategy: str = "lockstep"


@dataclass(frozen=True)
class MarketplaceClaudeConfig:
    """Claude-specific marketplace output configuration."""

    output: str = ".claude-plugin/marketplace.json"


@dataclass(frozen=True)
class MarketplaceCodexConfig:
    """Codex-specific marketplace output configuration."""

    output: str = MARKETPLACE_OUTPUTS["codex"].default_output


@dataclass(frozen=True)
class PackageEntry:
    """A single entry in the ``packages`` list.

    Attributes that are Anthropic pass-through (``description``,
    ``homepage``, ``tags``) are stored alongside APM-only attributes
    (``subdir``, ``version``, ``ref``, ``tag_pattern``,
    ``include_prerelease``) so the builder can partition them at
    compile time.

    ``is_local`` is derived by the loader from the ``source`` field --
    a leading ``./`` marks a local-path package that skips git
    resolution.
    """

    name: str
    source: str
    # APM-only fields
    subdir: str | None = None
    version: str | None = None
    ref: str | None = None
    tag_pattern: str | None = None
    include_prerelease: bool = False
    # Anthropic pass-through fields
    description: str | None = None
    homepage: str | None = None
    tags: tuple[str, ...] = ()
    # ``author`` is normalized to a Claude-Code-compliant object:
    # ``{"name": str, "email"?: str, "url"?: str}``. Accepts either a
    # bare string (treated as ``name``) or a mapping at parse time.
    author: Mapping[str, str] | None = None
    license: str | None = None
    repository: str | None = None
    # Marketplace category metadata. Emitted only by output formats that
    # consume categories, currently Codex repo marketplace output.
    category: str | None = None
    # Derived (set by loader, not by user)
    is_local: bool = False
    # Optional non-default git host parsed from ``source`` of the form
    # ``host.tld/owner/repo``. ``None`` means use the default host
    # (``GITHUB_HOST`` env or ``github.com``).
    host: str | None = None


@dataclass(frozen=True)
class MarketplaceOutputSpec:
    """Resolved specification for one marketplace output format.

    Produced by the map-form ``outputs:`` parser. When ``path_explicit``
    is True, the manifest set an explicit ``path:`` value (vs. the
    profile default).
    """

    name: str
    """Format name (matches a key in ``MARKETPLACE_OUTPUTS``)."""

    path: str
    """Resolved output path (explicit or profile default)."""

    path_explicit: bool = False
    """True if the user set an explicit ``path:`` in the outputs map."""


@dataclass(frozen=True)
class MarketplaceConfig:
    """Parsed marketplace configuration.

    May originate from apm.yml's ``marketplace:`` block (current) or
    from a standalone ``marketplace.yml`` (legacy, deprecated).

    ``metadata`` is stored as a plain ``dict`` preserving the original
    key casing so the builder can forward it verbatim to
    ``marketplace.json``.

    Override flags (``*_overridden``) record whether the marketplace
    block explicitly set each inheritable field.  The builder uses
    these flags to decide whether to emit ``description``/``version``
    at the top level of ``marketplace.json`` -- per the Anthropic
    azure-skills convention, inherited values are omitted from output.
    """

    name: str
    description: str
    version: str
    owner: MarketplaceOwner
    output: str = ".claude-plugin/marketplace.json"
    outputs: tuple[str, ...] = ("claude",)
    claude: MarketplaceClaudeConfig = field(default_factory=MarketplaceClaudeConfig)
    codex: MarketplaceCodexConfig = field(default_factory=MarketplaceCodexConfig)
    metadata: dict[str, Any] = field(default_factory=dict)
    build: MarketplaceBuild = field(default_factory=MarketplaceBuild)
    versioning: MarketplaceVersioning = field(default_factory=MarketplaceVersioning)
    source_base: str | None = None
    packages: tuple[PackageEntry, ...] = ()
    output_specs: tuple[MarketplaceOutputSpec, ...] = ()
    warnings: tuple[str, ...] = ()
    # Origin tracking + override-detection metadata
    source_path: Path | None = None
    is_legacy: bool = False
    name_overridden: bool = False
    description_overridden: bool = False
    version_overridden: bool = False
