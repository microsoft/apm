"""Credential binding for hosted HTTPS marketplace catalogs.

The binding is the user's own registration: ``apm marketplace add URL
--name NAME`` binds NAME to that catalog URL (``credential_bound``). Only a
bound registration ever resolves an ``APM_MARKETPLACE_*_<NAME>`` credential,
and only through the marketplace-only
:meth:`~apm_cli.core.auth.AuthResolver.resolve_marketplace_catalog_credential`
entry point. A name chosen by the catalog itself (``manifest.name``) never
selects a credential.

This module owns three marketplace-side steps:

* :func:`catalog_credential` -- the credential decision for one fetch.
* :func:`require_binding_available` -- ``marketplace add`` fails closed before
  any network access when the name is bound to another URL or its variables
  would be ambiguous.
* :func:`access_denied_error` -- maps an auth-classified catalog response to
  the denied-access error that names the variable to set.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable

from ..core.auth import AuthResolver
from ..core.auth_outcome import CATALOG_HOST_KIND, classify_auth_outcome
from ..core.catalog_credentials import (
    CatalogCredential,
    CatalogCredentialError,
    anonymous_catalog_credential,
    require_unambiguous_name,
)
from ..core.remediation import MARKETPLACE_CREDENTIAL_BIND
from .errors import (
    MarketplaceAccessDeniedError,
    MarketplaceBindingConflictError,
    MarketplaceCredentialError,
)
from .models import MarketplaceSource


def _bound_peer_names(name: str, registered: Iterable[MarketplaceSource]) -> list[str]:
    lower = name.lower()
    return [s.name for s in registered if s.credential_bound and s.name.lower() != lower]


def catalog_credential(source: MarketplaceSource) -> CatalogCredential:
    """Return the credential decision for fetching *source*'s hosted catalog.

    An unbound registration is always anonymous. Raises
    :class:`MarketplaceCredentialError` for ambiguous or malformed settings.
    """
    if not source.credential_bound:
        return anonymous_catalog_credential(source.name, source.url, bound=False)
    from .registry import get_registered_marketplaces

    try:
        return AuthResolver.resolve_marketplace_catalog_credential(
            marketplace=source.name,
            binding_url=source.url,
            peer_names=_bound_peer_names(source.name, get_registered_marketplaces()),
        )
    except CatalogCredentialError as exc:
        # ``from None``: no chained frame may carry a credential value.
        raise MarketplaceCredentialError(source.name, str(exc)) from None


def require_binding_available(name: str, url: str, registered: Iterable[MarketplaceSource]) -> None:
    """Fail closed when binding *name* to *url* conflicts with the registry.

    Raises:
        MarketplaceBindingConflictError: *name* is already bound to another URL.
        MarketplaceCredentialError: another bound name maps to the same
            variables.
    """
    registered = tuple(registered)
    lower = name.lower()
    for existing in registered:
        if existing.name.lower() == lower and existing.credential_bound and existing.url != url:
            raise MarketplaceBindingConflictError(existing.name, existing.url)
    try:
        require_unambiguous_name(name, _bound_peer_names(name, registered))
    except CatalogCredentialError as exc:
        raise MarketplaceCredentialError(name, str(exc)) from None


def _denied_message(credential: CatalogCredential, detail: str) -> str:
    name = credential.marketplace
    head = f"Access to marketplace '{name}' was denied ({detail})"
    hint = credential.variables.setup_hint()
    if not credential.bound:
        # Braces would read as unbound placeholders; %7B/%7D name the same URL.
        url = credential.binding_url.replace("{", "%7B").replace("}", "%7D")
        variables = credential.variables
        remedy = MARKETPLACE_CREDENTIAL_BIND.bind(
            url=shlex.quote(url),
            name=shlex.quote(name),
            token=variables.token,
            user=variables.user,
            password=variables.password,
        )
        return (
            f"{head}. APM sends catalog credentials only to a catalog you "
            f"registered by name: {remedy.clause()}."
        )
    if credential.presented:
        return (
            f"{head} for the credential from {credential.source_variable}. Check "
            "that it is current and grants read access to this catalog."
        )
    return f"{head}: no credential is set. {hint}"


def access_denied_error(
    credential: CatalogCredential, response: object
) -> MarketplaceAccessDeniedError | None:
    """Return the denied-access error for an auth-failed catalog response."""
    outcome = classify_auth_outcome(
        host_kind=CATALOG_HOST_KIND,
        response=response,
        credential_presented=credential.presented,
    )
    if not outcome.is_auth_failure:
        return None
    return MarketplaceAccessDeniedError(
        credential.marketplace,
        _denied_message(credential, outcome.detail or "authentication required"),
        status=outcome.status,
    )
