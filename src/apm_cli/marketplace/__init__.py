"""Marketplace integration for plugin discovery and governance."""

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
from .resolver import parse_marketplace_ref, resolve_marketplace_plugin
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
from .builder import (
    BuildOptions,
    BuildReport,
    MarketplaceBuilder,
    ResolvedPackage,
)
from .publisher import (
    ConsumerTarget,
    MarketplacePublisher,
    PublishOutcome,
    PublishPlan,
    TargetResult,
)
from .pr_integration import PrIntegrator, PrResult, PrState
from .ref_resolver import RefResolver, RemoteRef
from .semver import SemVer, parse_semver, satisfies_range
from .tag_pattern import build_tag_regex, render_tag

__all__ = [
    "MarketplaceError",
    "MarketplaceFetchError",
    "MarketplaceNotFoundError",
    "MarketplaceYmlError",
    "PluginNotFoundError",
    "BuildError",
    "GitLsRemoteError",
    "HeadNotAllowedError",
    "NoMatchingVersionError",
    "OfflineMissError",
    "RefNotFoundError",
    "MarketplaceManifest",
    "MarketplacePlugin",
    "MarketplaceSource",
    "parse_marketplace_json",
    "parse_marketplace_ref",
    "resolve_marketplace_plugin",
    "MarketplaceBuild",
    "MarketplaceConfig",
    "MarketplaceOwner",
    "MarketplaceYml",
    "PackageEntry",
    "load_marketplace_from_apm_yml",
    "load_marketplace_from_legacy_yml",
    "load_marketplace_yml",
    "BuildOptions",
    "BuildReport",
    "MarketplaceBuilder",
    "ResolvedPackage",
    "ConsumerTarget",
    "MarketplacePublisher",
    "PublishOutcome",
    "PublishPlan",
    "TargetResult",
    "PrIntegrator",
    "PrResult",
    "PrState",
    "RefResolver",
    "RemoteRef",
    "SemVer",
    "parse_semver",
    "satisfies_range",
    "build_tag_regex",
    "render_tag",
]
