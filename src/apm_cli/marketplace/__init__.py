"""Marketplace integration for plugin discovery and governance."""

# pylint: disable=duplicate-code  # __all__ re-exports intentionally mirror commands/marketplace surface

from .builder import (
    BuildOptions,
    BuildReport,
    MarketplaceBuilder,
    ResolvedPackage,
)
from .errors import (
    BuildError,
    GitLsRemoteError,
    HeadNotAllowedError,
    MarketplaceError,
    MarketplaceFetchError,
    MarketplaceNotFoundError,
    MarketplaceYmlError,
    NoMatchingVersionError,
    OfflineMissError,
    PluginNotFoundError,
    RefNotFoundError,
)
from .models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
    parse_marketplace_json,
)
from .ref_resolver import RefResolver, RemoteRef
from .resolver import parse_marketplace_ref, resolve_marketplace_plugin
from .semver import SemVer, parse_semver, satisfies_range
from .tag_pattern import build_tag_regex, render_tag
from .yml_schema import (
    MarketplaceBuild,
    MarketplaceConfig,
    MarketplaceOwner,
    MarketplaceYml,
    PackageEntry,
    load_marketplace_from_apm_yml,
    load_marketplace_from_legacy_yml,
    load_marketplace_yml,
)

__all__ = [
    "BuildError",
    "BuildOptions",
    "BuildReport",
    "GitLsRemoteError",
    "HeadNotAllowedError",
    "MarketplaceBuild",
    "MarketplaceBuilder",
    "MarketplaceConfig",
    "MarketplaceError",
    "MarketplaceFetchError",
    "MarketplaceManifest",
    "MarketplaceNotFoundError",
    "MarketplaceOwner",
    "MarketplacePlugin",
    "MarketplaceSource",
    "MarketplaceYml",
    "MarketplaceYmlError",
    "NoMatchingVersionError",
    "OfflineMissError",
    "PackageEntry",
    "PluginNotFoundError",
    "RefNotFoundError",
    "RefResolver",
    "RemoteRef",
    "ResolvedPackage",
    "SemVer",
    "build_tag_regex",
    "load_marketplace_from_apm_yml",
    "load_marketplace_from_legacy_yml",
    "load_marketplace_yml",
    "parse_marketplace_json",
    "parse_marketplace_ref",
    "parse_semver",
    "render_tag",
    "resolve_marketplace_plugin",
    "satisfies_range",
]
