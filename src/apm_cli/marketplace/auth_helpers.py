"""Shared auth helpers for marketplace commands.

Both ``apm pack`` (``MarketplaceBuilder``) and ``apm marketplace check`` need
to resolve a per-host token before running ``git ls-remote`` against a
non-default host (self-managed GitLab, GHES, ADO, Bitbucket DC). This module
is the single home for that logic so the two paths cannot drift.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.auth import AuthResolver

logger = logging.getLogger(__name__)


def resolve_token_for_host(
    host: str,
    *,
    offline: bool = False,
    auth_resolver: AuthResolver | None = None,
) -> str | None:
    """Resolve an auth token for a non-default *host* via ``AuthResolver``.

    Returns ``None`` -- letting ``git`` fall back to ambient credentials --
    when offline, when no token is configured for the host, or when
    ``AuthResolver`` raises. Never raises.

    Pass *auth_resolver* to reuse a cached resolver across many calls (the
    builder does this during metadata prefetch); otherwise a fresh one is
    created per call.
    """
    if offline:
        return None
    try:
        from ..core.auth import AuthResolver  # lazy import to avoid cycles

        resolver = auth_resolver if auth_resolver is not None else AuthResolver()
        ctx = resolver.resolve(host)
        if ctx.token:
            logger.debug("Resolved token for host %s (source=%s)", host, ctx.source)
            return ctx.token
    except Exception:
        logger.debug("Could not resolve token for host %s", host, exc_info=True)
    return None
