"""Enterprise bootstrap mirror environment helpers."""

import os

from apm_cli.utils.path_security import PathTraversalError, validate_path_segments

APM_RELEASE_BASE_URL = "APM_RELEASE_BASE_URL"
APM_RELEASE_METADATA_URL = "APM_RELEASE_METADATA_URL"
APM_INSTALLER_BASE_URL = "APM_INSTALLER_BASE_URL"
APM_PYPI_INDEX_URL = "APM_PYPI_INDEX_URL"
APM_NO_DIRECT_FALLBACK = "APM_NO_DIRECT_FALLBACK"
_VERSION_ENV_VAR = "VERSION"

_PUBLIC_GITHUB_URL = "https://github.com"
_TRUE_VALUES = {"1", "true", "yes", "on"}


def get_env_url(name: str) -> str | None:
    """Return a stripped URL env var value, or None when unset/empty."""
    value = os.environ.get(name, "").strip().strip('"').strip("'")
    return value.rstrip("/") if value else None


def env_flag_enabled(name: str) -> bool:
    """Return True when an env flag is set to a conventional truthy value."""
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def no_direct_fallback_enabled() -> bool:
    """Return True when public direct fallback must be disabled."""
    return env_flag_enabled(APM_NO_DIRECT_FALLBACK)


def append_url_path(base_url: str, *parts: str) -> str:
    """Join a base URL and path parts without unsafe dot segments."""
    cleaned: list[str] = []
    for part in parts:
        if not part:
            continue
        segment = part.strip("/")
        try:
            validate_path_segments(segment, context="URL path part")
        except PathTraversalError as exc:
            raise ValueError("URL path parts must not contain dot segments") from exc
        if segment:
            cleaned.append(segment)
    if not cleaned:
        return base_url.rstrip("/")
    return "/".join([base_url.rstrip("/"), *cleaned])


def get_release_metadata_url() -> str | None:
    """Return the mirrored release metadata URL override, if configured."""
    return get_env_url(APM_RELEASE_METADATA_URL)


def get_release_base_url() -> str | None:
    """Return the mirrored release asset base URL override, if configured."""
    return get_env_url(APM_RELEASE_BASE_URL)


def get_installer_base_url() -> str | None:
    """Return the mirrored installer script base URL override, if configured."""
    return get_env_url(APM_INSTALLER_BASE_URL)


def get_pypi_index_url() -> str | None:
    """Return the mirrored PyPI index URL override, if configured."""
    return get_env_url(APM_PYPI_INDEX_URL)


def is_public_github_url(github_url: str | None) -> bool:
    """Return True when the configured GitHub URL is the public host."""
    return (github_url or _PUBLIC_GITHUB_URL).rstrip("/") == _PUBLIC_GITHUB_URL


def release_metadata_public_lookup_blocked(github_url: str | None = None) -> bool:
    """Return True when latest-release lookup would violate no-direct mode."""
    return (
        no_direct_fallback_enabled()
        and is_public_github_url(github_url)
        and get_release_metadata_url() is None
        and not os.environ.get(_VERSION_ENV_VAR, "").strip()
    )


def installer_public_download_blocked(github_url: str | None = None) -> bool:
    """Return True when installer download would violate no-direct mode."""
    return (
        no_direct_fallback_enabled()
        and is_public_github_url(github_url)
        and get_installer_base_url() is None
    )


def build_mirrored_release_asset_url(tag_name: str, asset_name: str) -> str | None:
    """Build the mirrored release asset URL for a tag and asset name."""
    base_url = get_release_base_url()
    if base_url is None:
        return None
    return append_url_path(base_url, tag_name, asset_name)
