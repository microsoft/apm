"""Marketplace integration for plugin discovery and governance."""

# pylint: disable=duplicate-code  # __all__ re-exports intentionally mirror commands/marketplace surface

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

_LAZY = {
    "BuildOptions": ("builder", "BuildOptions"),
    "BuildReport": ("builder", "BuildReport"),
    "MarketplaceBuilder": ("builder", "MarketplaceBuilder"),
    "ResolvedPackage": ("builder", "ResolvedPackage"),
    "BuildError": ("errors", "BuildError"),
    "GitLsRemoteError": ("errors", "GitLsRemoteError"),
    "HeadNotAllowedError": ("errors", "HeadNotAllowedError"),
    "MarketplaceError": ("errors", "MarketplaceError"),
    "MarketplaceFetchError": ("errors", "MarketplaceFetchError"),
    "MarketplaceNotFoundError": ("errors", "MarketplaceNotFoundError"),
    "MarketplaceYmlError": ("errors", "MarketplaceYmlError"),
    "NoMatchingVersionError": ("errors", "NoMatchingVersionError"),
    "OfflineMissError": ("errors", "OfflineMissError"),
    "PluginNotFoundError": ("errors", "PluginNotFoundError"),
    "RefNotFoundError": ("errors", "RefNotFoundError"),
    "MarketplaceManifest": ("models", "MarketplaceManifest"),
    "MarketplacePlugin": ("models", "MarketplacePlugin"),
    "MarketplaceSource": ("models", "MarketplaceSource"),
    "parse_marketplace_json": ("models", "parse_marketplace_json"),
    "RefResolver": ("ref_resolver", "RefResolver"),
    "RemoteRef": ("ref_resolver", "RemoteRef"),
    "parse_marketplace_ref": ("resolver", "parse_marketplace_ref"),
    "resolve_marketplace_plugin": ("resolver", "resolve_marketplace_plugin"),
    "SemVer": ("semver", "SemVer"),
    "parse_semver": ("semver", "parse_semver"),
    "satisfies_range": ("semver", "satisfies_range"),
    "build_tag_regex": ("tag_pattern", "build_tag_regex"),
    "render_tag": ("tag_pattern", "render_tag"),
    "MarketplaceBuild": ("yml_schema", "MarketplaceBuild"),
    "MarketplaceConfig": ("yml_schema", "MarketplaceConfig"),
    "MarketplaceOwner": ("yml_schema", "MarketplaceOwner"),
    "MarketplaceYml": ("yml_schema", "MarketplaceYml"),
    "PackageEntry": ("yml_schema", "PackageEntry"),
    "load_marketplace_from_apm_yml": ("yml_schema", "load_marketplace_from_apm_yml"),
    "load_marketplace_from_legacy_yml": ("yml_schema", "load_marketplace_from_legacy_yml"),
    "load_marketplace_yml": ("yml_schema", "load_marketplace_yml"),
}


def __getattr__(name):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f".{target[0]}", __name__), target[1])
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
