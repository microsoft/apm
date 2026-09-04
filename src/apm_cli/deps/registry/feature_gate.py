"""Experimental feature gate for REST-based APM package registries."""

from __future__ import annotations

FLAG_NAME = "registries"
DISPLAY_NAME = "registries"
ENABLE_COMMAND = f"apm experimental enable {DISPLAY_NAME}"


class PackageRegistryFeatureDisabledError(ValueError):
    """Raised when registries behavior is used without opt-in."""


def is_package_registry_enabled(*, create_config: bool = True) -> bool:
    """Return whether the registries experimental flag is enabled."""
    from apm_cli.core.experimental import is_enabled

    return is_enabled(FLAG_NAME, create_config=create_config)


def require_package_registry_enabled(
    action: str = "APM package registries",
    *,
    create_config: bool = True,
) -> None:
    """Raise a consistent error if REST package registries are disabled."""
    if is_package_registry_enabled(create_config=create_config):
        return
    raise PackageRegistryFeatureDisabledError(
        f"{action} requires the experimental {DISPLAY_NAME} feature. "
        f"Enable with: {ENABLE_COMMAND}. "
        "Run 'apm experimental list' to see available experimental features."
    )
