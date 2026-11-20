"""Ownership check for sending the proxy registry token over http (#4314).

``deps/registry_proxy.http_token_allowed`` alone decides whether
``PROXY_REGISTRY_TOKEN`` may travel over ``http://``: only to a loopback or
RFC 1918 host (``utils/net.is_local_network_host``), or with the explicit
``PROXY_REGISTRY_ALLOW_HTTP=1`` opt-in. ``RegistryConfig.from_env`` drops the
token when it says no, and no other module reads the opt-in.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _count_checks,
    _forbid_scan,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-proxy-http-token"
_OWNER = "src/apm_cli/deps/registry_proxy.py"
_NET = "src/apm_cli/utils/net.py"
_OPT_IN = re.compile(r"PROXY_REGISTRY_ALLOW_HTTP")


def _check_proxy_http_token(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep the http token decision in one owner and its one consumer."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _OWNER,
            (
                ("re", r"^def http_token_allowed\(", 1, "eq"),
                ("sub", "if token and not http_token_allowed(parsed.scheme, host):", 1, "eq"),
                ("sub", "return is_local_network_host(host) or ", 1, "eq"),
            ),
            "the proxy token travels over http only when http_token_allowed says so",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _NET,
            (("re", r"^def is_local_network_host\(", 1, "eq"),),
            "local-network host classification must stay owned by utils/net.py",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_OWNER}),
            _OPT_IN,
            "Only deps/registry_proxy.py may read PROXY_REGISTRY_ALLOW_HTTP",
            exempt=True,
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="The proxy registry token travels over http only per http_token_allowed.",
        check=_check_proxy_http_token,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
