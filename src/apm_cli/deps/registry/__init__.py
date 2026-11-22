"""Dedicated registry API resolver.

Additive resolver mode that fetches APM packages over a REST registry contract
(see docs/proposals/registry-api.md). Sits alongside the existing Git resolver;
opt-in via ``apm experimental enable registries`` before using the
top-level ``registries:`` block in ``apm.yml``.

This package is intentionally separate from ``src/apm_cli/registry/`` (the MCP
registry client) — the two address different concepts and must not be confused.
"""

# PEP 562 lazy re-exports: ``import apm_cli.deps.registry.feature_gate`` (on
# every ``apm install`` dispatch) must not pull the resolver, the HTTP client
# and the bundle graph behind them. Names resolve on first attribute access.
_LAZY_EXPORTS = {
    "RegistryAuthContext": ".auth",
    "make_auth_context": ".auth",
    "registry_token_env_var": ".auth",
    "resolve_registry_basic": ".auth",
    "resolve_registry_token": ".auth",
    "RegistryClient": ".client",
    "RegistryError": ".client",
    "VersionEntry": ".client",
    "load_merged_registries": ".config_loader",
    "resolve_effective_registries": ".config_loader",
    "extract_archive": ".extractor",
    "extract_tarball": ".extractor",
    "extract_zip": ".extractor",
    "verify_sha256": ".extractor",
    "DISPLAY_NAME": ".feature_gate",
    "ENABLE_COMMAND": ".feature_gate",
    "FLAG_NAME": ".feature_gate",
    "PackageRegistryFeatureDisabledError": ".feature_gate",
    "is_package_registry_enabled": ".feature_gate",
    "require_package_registry_enabled": ".feature_gate",
    "RegistryPackageResolver": ".resolver",
    "is_semver_range": ".semver",
    "match_version": ".semver",
}

__all__ = [
    "DISPLAY_NAME",
    "ENABLE_COMMAND",
    "FLAG_NAME",
    "PackageRegistryFeatureDisabledError",
    "RegistryAuthContext",
    "RegistryClient",
    "RegistryError",
    "RegistryPackageResolver",
    "VersionEntry",
    "extract_archive",
    "extract_tarball",
    "extract_zip",
    "is_package_registry_enabled",
    "is_semver_range",
    "load_merged_registries",
    "make_auth_context",
    "match_version",
    "registry_token_env_var",
    "require_package_registry_enabled",
    "resolve_effective_registries",
    "resolve_registry_basic",
    "resolve_registry_token",
    "verify_sha256",
]


def __getattr__(name: str):
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
