"""Hosted marketplace catalog credentials stay in one closed world.

Ports the ``marketplace-catalog-credentials`` decision in
``.apm/architecture/owners/transport-auth-platform.json``:
``core/catalog_credentials.py`` owns the ``APM_MARKETPLACE_*`` variables and
the credential value, ``AuthResolver.resolve_marketplace_catalog_credential``
is the only entry point (no Git, host, registry or ``.netrc`` fallback), and
``marketplace/catalog_binding.py`` is its only caller.

The guard is closed-world, not a caller list: every product module is scanned
and only the inventory below may spell the variable prefixes, call the entry
points or import the owner module. A new consumer -- for example a package or
Git download reaching for a catalog credential -- fails until it is reviewed
and added here. Owner contracts pin the binding gate, the per-hop scope, the
disabled ambient credentials and the denied-access handling.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _forbid_scan,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-marketplace-catalog-credentials"
_OWNER = "src/apm_cli/core/catalog_credentials.py"
_AUTH = "src/apm_cli/core/auth.py"
_BINDING = "src/apm_cli/marketplace/catalog_binding.py"
_CLIENT = "src/apm_cli/marketplace/client.py"
_REGISTRY_AUTH = "src/apm_cli/deps/registry/auth.py"
_HTTP_OWNER = "src/apm_cli/utils/http_credentials.py"

# (pattern, closed-world allowed paths, message)
_CLOSED_WORLD: tuple[tuple[re.Pattern[str], frozenset[str], str], ...] = (
    (
        re.compile(r"APM_MARKETPLACE_(?:TOKEN|USER|PASS)_"),
        frozenset({_OWNER}),
        "APM_MARKETPLACE_* variables are read only by core/catalog_credentials.py",
    ),
    (
        re.compile(r"\bresolve_catalog_credential\("),
        frozenset({_OWNER, _AUTH}),
        "Catalog credentials resolve only through "
        "AuthResolver.resolve_marketplace_catalog_credential",
    ),
    (
        re.compile(r"\bresolve_marketplace_catalog_credential\("),
        frozenset({_AUTH, _BINDING}),
        "Only marketplace/catalog_binding.py may resolve a catalog credential",
    ),
    (
        re.compile(r"\bcatalog_credential\("),
        frozenset({_BINDING, _CLIENT}),
        "Only the hosted-catalog fetch may obtain a catalog credential; "
        "package and Git requests never carry it",
    ),
    (
        re.compile(r"\bcatalog_credentials\b"),
        frozenset({_OWNER, _AUTH, _BINDING, _CLIENT, _REGISTRY_AUTH}),
        "core/catalog_credentials.py has a closed consumer inventory; review "
        "and register a new consumer in this guard",
    ),
)

_OWNER_CONTRACTS: Mapping[str, tuple[str, ...]] = {
    _OWNER: (
        "require_unambiguous_name(marketplace, peer_names)",
        "_authorization: str | None = field(default=None, repr=False, compare=False)",
        "def catalog_scope_url(",
    ),
    _AUTH: (
        "def resolve_marketplace_catalog_credential(",
        "environ=os.environ,",
    ),
    _BINDING: (
        "if not source.credential_bound:",
        "return anonymous_catalog_credential(source.name, source.url, bound=False)",
        "raise MarketplaceCredentialError(source.name, str(exc)) from None",
        "and existing.credential_bound and existing.url != url:",
        "host_kind=CATALOG_HOST_KIND,",
    ),
    _CLIENT: (
        "credential=catalog_credential(source),",
        "base_url=credential.scope_url if credential.presented else None,",
        "bind_query=credential.presented,",
        "ambient_credentials=False,",
        "denied = access_denied_error(credential, resp)",
        "if isinstance(exc, MarketplaceAuthError):",
    ),
    _HTTP_OWNER: (
        "reason in (REFUSED_OUTSIDE_ORIGIN, REFUSED_OUTSIDE_BASE_PATH, REFUSED_OUTSIDE_QUERY)",
        "if bind_query and _comparable_query(parts.query) != _comparable_query(",
    ),
}


def _check_catalog_credentials(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep hosted-catalog credentials inside their owner and one consumer."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    for pattern, allowed, message in _CLOSED_WORLD:
        findings.extend(
            _forbid_scan(
                provider,
                inventory,
                _RULE_ID,
                _src_python(provider, exclude=allowed),
                pattern,
                message,
                exempt=False,
            )
        )
    for path, needles in sorted(_OWNER_CONTRACTS.items()):
        findings.extend(
            _require_subs(
                provider,
                inventory,
                _RULE_ID,
                path,
                needles,
                "Hosted-catalog credential contract changed",
            )
        )
    return tuple(findings)


RULES = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description=(
            "Hosted marketplace catalog credentials have one owner, one entry "
            "point and one consumer."
        ),
        check=_check_catalog_credentials,
    ),
)
COLLECTORS: tuple[object, ...] = ()
