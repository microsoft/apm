"""Experimental feature gate for REST-based APM package registries."""

from __future__ import annotations

FLAG_NAME = "registries"
DISPLAY_NAME = "registries"
ENABLE_COMMAND = f"apm experimental enable {DISPLAY_NAME}"


class PackageRegistryFeatureDisabledError(ValueError):
    """Raised when registries behavior is used without opt-in."""


class UserRegistryConfigDisabledError(PackageRegistryFeatureDisabledError):
    """Raised when $APM_HOME configures registries but the feature is off.

    The project manifest is not at fault, so callers render the message as-is.
    """


def is_package_registry_enabled(*, create_config: bool = True) -> bool:
    """Return whether the registries experimental flag is enabled."""
    from apm_cli.core.experimental import is_enabled

    return is_enabled(FLAG_NAME, create_config=create_config)


def require_package_registry_enabled(
    action: str = "APM package registries",
    *,
    create_config: bool = True,
    config_source: str | None = None,
) -> None:
    """Raise a consistent error if REST package registries are disabled.

    ``config_source`` names the $APM_HOME file that configures registries
    when the requirement does not come from the project manifest (#4175).
    """
    if is_package_registry_enabled(create_config=create_config):
        return
    error = PackageRegistryFeatureDisabledError
    if config_source:
        action = f"{action} in {config_source}"
        error = UserRegistryConfigDisabledError
    raise error(
        f"{action} requires the experimental {DISPLAY_NAME} feature. "
        f"Run '{ENABLE_COMMAND}' to enable it. "
        "Run 'apm experimental list' to see available experimental features."
    )
