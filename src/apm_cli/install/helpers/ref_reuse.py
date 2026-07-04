"""Run-scoped RefResolver reuse for semver resolution.

Extracted from :mod:`apm_cli.install.phases.resolve` to keep that phase
module within its LOC budget (see
``tests/unit/install/test_architecture_invariants.py``).

Multiple semver deps from the same upstream repo should share one
``RefResolver`` so its per-instance ``git ls-remote`` tag listing is fetched
once per repo instead of once per dep.
"""

from __future__ import annotations

import hashlib
from typing import Any


def _token_fingerprint(token: str | None) -> str | None:
    """Return a non-reversible fingerprint of ``token`` for use as a cache key.

    The cache lives on ``InstallContext``; keying by the raw PAT would leak
    the credential into any ``repr(ctx)`` / debug dump / dict-key trace. A
    truncated SHA-256 keeps distinct tokens in distinct buckets without
    storing the secret. ``None`` (unauthenticated) maps to ``None``.
    """
    if token is None:
        return None
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def resolve_dep_token(dep_ref: Any, auth_resolver: Any) -> str | None:
    """Resolve the per-dep token via AuthResolver for use by ``git ls-remote``.

    Uses the same credential source the downstream clone will use. Without
    this threading, ls-remote on a private repo would rely on the host's git
    credential helper (present on dev laptops, absent in CI). Best-effort:
    on any failure the unauth path remains and the downstream clone surfaces
    the real auth error with its own diagnostic.
    """
    if auth_resolver is None:
        return None
    try:
        auth_ctx = auth_resolver.resolve_for_dep(dep_ref)
        return auth_ctx.token if auth_ctx is not None else None
    except Exception:
        return None


def get_shared_ref_resolver(
    host: str | None,
    token: str | None,
    cache: dict[Any, Any] | None,
    lock: Any = None,
) -> Any:
    """Return a ``RefResolver`` for ``(host, token)``, reused across a run.

    When ``cache`` is provided, resolvers are memoized so the second and
    later deps from a repo reuse the instance (and its ref cache). The cache
    key is ``(normalized_host, fingerprint(token))`` -- a non-reversible token
    fingerprint, never the raw PAT, so the credential is not exposed via the
    context object this cache lives on. ``host`` is normalized to ``None``
    meaning "use RefResolver default (github.com)", so a dep written with an
    explicit ``host='github.com'`` and one with no host collapse to the same
    cache bucket. When ``lock`` is also provided, the get-or-create runs under
    it -- required because the BFS download callback runs on a worker pool,
    where unguarded concurrent first-touches would each build a resolver and
    defeat the dedup. ``cache=None`` (the default caller behavior) builds a
    fresh resolver per call, preserving the legacy one-per-dep path. Token
    rotation mid-run is intentionally unsupported: once a resolver is cached,
    its embedded token is fixed for the lifetime of the run (APM installs are
    short-lived; tokens do not rotate mid-process).
    """
    from apm_cli.marketplace.ref_resolver import RefResolver

    # Normalize the default github.com host so deps that omit host and deps
    # that spell out 'github.com' explicitly share the same cache bucket.
    _DEFAULT_HOST = "github.com"
    canonical_host = host if host and host != _DEFAULT_HOST else None

    if cache is None:
        return RefResolver(host=host, token=token)

    key = (canonical_host, _token_fingerprint(token))
    if lock is not None:
        with lock:
            resolver = cache.get(key)
            if resolver is None:
                resolver = RefResolver(host=host, token=token)
                cache[key] = resolver
            return resolver

    resolver = cache.get(key)
    if resolver is None:
        resolver = RefResolver(host=host, token=token)
        cache[key] = resolver
    return resolver
