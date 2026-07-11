"""Canonical registry for remote Git host capabilities."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from apm_cli.utils.github_host import (
    is_azure_devops_hostname,
    is_gitlab_hostname,
    is_valid_fqdn,
)

ApiBaseBuilder = Callable[[str], str]
HostMatcher = Callable[[str], bool]


@dataclass(frozen=True)
class HostProviderDescriptor:
    """Describe host classification, credentials, and API capabilities."""

    kind: str
    matcher: HostMatcher
    api_base: ApiBaseBuilder
    has_public_repos: bool
    credential_purpose: str
    allow_credential_helper: bool = True
    manifest_types: tuple[str, ...] = ()


def _github_api(_host: str) -> str:
    return "https://api.github.com"


def _ado_api(_host: str) -> str:
    return "https://dev.azure.com"


def _api_v3(host: str) -> str:
    return f"https://{host}/api/v3"


def _gitlab_api(host: str) -> str:
    return "https://gitlab.com/api/v4" if host == "gitlab.com" else f"https://{host}/api/v4"


def _matches_github(host: str) -> bool:
    return host == "github.com"


def _matches_ghe_cloud(host: str) -> bool:
    return host.endswith(".ghe.com")


def _matches_ado(host: str) -> bool:
    return is_azure_devops_hostname(host)


def _matches_ghes(host: str) -> bool:
    configured = os.environ.get("GITHUB_HOST", "").lower()
    return bool(
        configured
        and configured == host
        and configured not in {"github.com", "gitlab.com"}
        and not configured.endswith(".ghe.com")
        and is_valid_fqdn(configured)
    )


def _matches_gitlab(host: str) -> bool:
    return is_gitlab_hostname(host)


def _matches_any(_host: str) -> bool:
    return True


_HOST_PROVIDERS = (
    HostProviderDescriptor(
        kind="github",
        matcher=_matches_github,
        api_base=_github_api,
        has_public_repos=True,
        credential_purpose="modules",
    ),
    HostProviderDescriptor(
        kind="ghe_cloud",
        matcher=_matches_ghe_cloud,
        api_base=_api_v3,
        has_public_repos=False,
        credential_purpose="modules",
    ),
    HostProviderDescriptor(
        kind="ado",
        matcher=_matches_ado,
        api_base=_ado_api,
        has_public_repos=True,
        credential_purpose="ado_modules",
        allow_credential_helper=False,
    ),
    HostProviderDescriptor(
        kind="ghes",
        matcher=_matches_ghes,
        api_base=_api_v3,
        has_public_repos=True,
        credential_purpose="modules",
    ),
    HostProviderDescriptor(
        kind="gitlab",
        matcher=_matches_gitlab,
        api_base=_gitlab_api,
        has_public_repos=True,
        credential_purpose="gitlab_modules",
        manifest_types=("gitlab",),
    ),
    HostProviderDescriptor(
        kind="generic",
        matcher=_matches_any,
        api_base=_api_v3,
        has_public_repos=True,
        credential_purpose="generic_modules",
    ),
)

HOST_PROVIDERS = MappingProxyType({provider.kind: provider for provider in _HOST_PROVIDERS})
_BACKEND_FACTORIES: dict[str, type[Any]] = {}


def host_provider_descriptors() -> tuple[HostProviderDescriptor, ...]:
    """Return providers in classification precedence order."""
    return _HOST_PROVIDERS


def accepted_host_types() -> tuple[str, ...]:
    """Return manifest host-type hints accepted by the registry."""
    return tuple(host_type for provider in _HOST_PROVIDERS for host_type in provider.manifest_types)


def classify_host_provider(
    host: str,
    *,
    host_type: str | None = None,
) -> HostProviderDescriptor:
    """Classify one host through the canonical provider registry."""
    normalized_host = host.lower()
    normalized_type = host_type.strip().lower() if isinstance(host_type, str) else ""
    if normalized_type:
        for provider in _HOST_PROVIDERS:
            if normalized_type in provider.manifest_types:
                return provider
        supported = ", ".join(accepted_host_types()) or "(none)"
        raise ValueError(
            f"Unsupported dependency host type: {normalized_type}. Supported values: {supported}"
        )
    for provider in _HOST_PROVIDERS:
        if provider.matcher(normalized_host):
            return provider
    raise RuntimeError(f"No host provider registered for {host!r}")


def register_host_backend(kind: str, backend_factory: type[Any]) -> None:
    """Register a native backend for one canonical host provider."""
    if kind not in HOST_PROVIDERS:
        raise ValueError(f"Cannot register backend for unknown host provider: {kind}")
    existing = _BACKEND_FACTORIES.get(kind)
    if existing is not None and existing is not backend_factory:
        raise ValueError(f"Host backend already registered for provider: {kind}")
    _BACKEND_FACTORIES[kind] = backend_factory


def host_backend_factory(kind: str) -> type[Any]:
    """Return the backend registered for a canonical provider."""
    try:
        return _BACKEND_FACTORIES[kind]
    except KeyError:
        raise RuntimeError(f"No host backend registered for provider: {kind}") from None
