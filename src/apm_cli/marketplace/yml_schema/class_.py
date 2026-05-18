# pylint: disable=duplicate-code
"""Dataclasses, loader, and validation for marketplace authoring config.

The marketplace publisher configuration may live in two places:

* (Preferred, current) inside ``apm.yml`` under a top-level
  ``marketplace:`` block.  Loaded via
  :func:`load_marketplace_from_apm_yml`.
* (Legacy, deprecated) inside a standalone ``marketplace.yml`` file.
  Loaded via :func:`load_marketplace_from_legacy_yml`.

Both paths produce the same immutable :class:`MarketplaceConfig`
dataclass that the builder consumes.

Key design rules
----------------
* **Anthropic pass-through preservation.**  The ``metadata`` block is
  stored as a plain ``dict`` with original key casing (e.g.
  ``pluginRoot`` stays ``pluginRoot``).  Unknown keys inside ``metadata``
  are preserved -- only the builder decides what is forwarded.
* **APM-only vs Anthropic separation.**  Build-time fields (``build``,
  ``version``, ``ref``, ``subdir``, ``tag_pattern``,
  ``include_prerelease``) live as explicit dataclass attributes so the
  builder can strip them cleanly.
* **Strict key sets.**  Unknown keys inside the marketplace block raise
  ``MarketplaceYmlError`` so typos are never silently ignored.  The
  apm.yml top-level is intentionally NOT strict here -- only the
  ``marketplace:`` subtree is validated by this module.
* **Local-path packages.**  ``source`` accepts ``./...`` paths in
  addition to ``owner/repo`` shape.  Local packages skip ref resolution.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import MarketplaceYmlError
from ..output_profiles import MARKETPLACE_OUTPUTS

__all__ = [
    "LOCAL_SOURCE_RE",
    "SOURCE_RE",
    "MarketplaceBuild",
    "MarketplaceClaudeConfig",
    "MarketplaceCodexConfig",
    "MarketplaceConfig",
    "MarketplaceOutputSpec",
    "MarketplaceOwner",
    "MarketplaceYml",  # backwards-compat alias
    "MarketplaceYmlError",
    "PackageEntry",
    "load_marketplace_from_apm_yml",
    "load_marketplace_from_legacy_yml",
    "load_marketplace_yml",
]

# ---------------------------------------------------------------------------
# Semver validation (matches codebase convention -- regex, no external lib)
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

# Source field accepts either ``owner/repo`` (remote) or ``./...`` (local
# path within the same repo).  Used by both yml_schema and yml_editor for
# source field validation.
SOURCE_RE = re.compile(r"^(?:[^/]+/[^/]+|\./.*)$")
LOCAL_SOURCE_RE = re.compile(r"^\./")

# Placeholder tokens accepted in ``tag_pattern`` / ``build.tagPattern``.
_TAG_PLACEHOLDERS = ("{version}", "{name}")

# ---------------------------------------------------------------------------
# Permitted key sets (strict mode)
# ---------------------------------------------------------------------------

_BUILD_KEYS = frozenset(
    {
        "tagPattern",
    }
)

_PACKAGE_ENTRY_KEYS = frozenset(
    {
        "name",
        "source",
        "subdir",
        "version",
        "ref",
        "tag_pattern",
        "include_prerelease",
        "description",
        "homepage",
        "tags",
        "author",
        "license",
        "repository",
        "keywords",
        "category",
    }
)

# Limits for keywords/tags array to prevent DoS via oversized manifests (S4).
_MAX_TAGS_COUNT = 50
_MAX_TAG_LENGTH = 100

# Keys permitted inside an ``author`` object (rejected if anything else
# present). Mirrors the Claude Code plugin manifest schema.
_AUTHOR_OBJECT_KEYS = frozenset({"name", "email", "url"})


def _parse_author(raw: Any, index: int) -> dict[str, str] | None:
    return _parse_helpers._parse_author(raw, index)


# Keys permitted inside the ``marketplace:`` block of apm.yml.  This is
# distinct from the legacy top-level keys (which include ``name``,
# ``description``, ``version`` -- those are inherited from apm.yml's
# top-level scalars in the new world).
_APM_MARKETPLACE_KEYS = frozenset(
    {
        "name",  # optional override of top-level apm.yml name
        "description",  # optional override of top-level apm.yml description
        "version",  # optional override of top-level apm.yml version
        "owner",
        "output",
        "outputs",
        "claude",
        "metadata",
        "build",
        "codex",
        "packages",
    }
)

_CLAUDE_KEYS = frozenset(
    {
        "output",
    }
)

_CODEX_KEYS = frozenset(
    {
        "output",
    }
)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


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
class MarketplaceClaudeConfig:
    """Claude-specific marketplace output configuration."""

    output: str = ".claude-plugin/marketplace.json"


@dataclass(frozen=True)
class MarketplaceCodexConfig:
    """Codex-specific marketplace output configuration."""

    output: str = MARKETPLACE_OUTPUTS["codex"].default_output


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
    packages: tuple[PackageEntry, ...] = ()
    output_specs: tuple[MarketplaceOutputSpec, ...] = ()
    warnings: tuple[str, ...] = ()
    # Origin tracking + override-detection metadata
    source_path: Path | None = None
    is_legacy: bool = False
    name_overridden: bool = False
    description_overridden: bool = False
    version_overridden: bool = False


# Backwards-compatibility alias for callers that still import
# ``MarketplaceYml``.  Will be removed in a future minor release.
MarketplaceYml = MarketplaceConfig


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_str(data: dict[str, Any], key: str, *, context: str = "") -> str:
    return _parse_helpers._require_str(data, key, context=context)


def _validate_semver(version: str, *, context: str = "version") -> None:
    return _parse_helpers._validate_semver(version, context=context)


def _validate_source(source: str, *, index: int) -> None:
    return _parse_helpers._validate_source(source, index=index)


def _validate_tag_pattern(pattern: str, *, context: str) -> None:
    return _parse_helpers._validate_tag_pattern(pattern, context=context)


def _check_unknown_keys(data: dict[str, Any], permitted: frozenset, *, context: str) -> None:
    return _parse_helpers._check_unknown_keys(data, permitted, context=context)


# ---------------------------------------------------------------------------
# Internal parse helpers
# ---------------------------------------------------------------------------


def _parse_owner(raw: Any) -> MarketplaceOwner:
    return _parse_helpers._parse_owner(raw)


def _parse_build(raw: Any) -> MarketplaceBuild:
    return _parse_helpers._parse_build(raw)


def _parse_claude(raw: Any, *, default_output: str) -> MarketplaceClaudeConfig:
    return _parse_helpers._parse_claude(raw, default_output=default_output)


def _parse_codex(raw: Any) -> MarketplaceCodexConfig:
    return _parse_helpers._parse_codex(raw)


def _parse_outputs(
    raw: Any, warnings_sink: list[str] | None = None
) -> tuple[tuple[str, ...], tuple[MarketplaceOutputSpec, ...]]:
    return _parse_helpers._parse_outputs(raw, warnings_sink)


def _parse_package_entry(raw: Any, index: int) -> PackageEntry:
    return _parse_helpers._parse_package_entry(raw, index)


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_marketplace_yml(path: Path) -> MarketplaceConfig:
    return _loaders.load_marketplace_yml(path)


def load_marketplace_from_legacy_yml(path: Path) -> MarketplaceConfig:
    return _loaders.load_marketplace_from_legacy_yml(path)


def load_marketplace_from_apm_yml(apm_yml_path: Path) -> MarketplaceConfig:
    return _loaders.load_marketplace_from_apm_yml(apm_yml_path)


# ---------------------------------------------------------------------------
# Shared internal helpers
# ---------------------------------------------------------------------------


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    return _loaders._read_yaml_mapping(path)


def _build_config(ctx: _loaders._BuildConfigInput) -> MarketplaceConfig:
    return _loaders._build_config(ctx)


from . import loaders as _loaders
from . import parse_helpers as _parse_helpers
