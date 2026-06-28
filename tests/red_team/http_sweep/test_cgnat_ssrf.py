"""r3-http-1: RFC 6598 carrier-grade NAT (100.64.0.0/10) is internal.

The stdlib ``ipaddress`` predicates classify CGNAT shared space as neither
is_private nor is_global, so an SSRF guard that only checks is_private /
is_loopback / is_link_local / is_reserved / is_multicast / is_unspecified
would dispatch a request there -- reaching a sibling tenant behind the
carrier NAT. _ip_is_internal must treat the whole /10 as internal, and the
host-level guard must refuse a literal in that range before any socket.
"""

from __future__ import annotations

import ipaddress

import pytest

from apm_cli.core.script_executors import _ip_is_internal, _ssrf_block_reason

CGNAT_LITERALS = [
    "100.64.0.0",  # network address
    "100.64.0.1",
    "100.100.100.100",  # mid-range
    "100.127.255.255",  # last usable in /10
]

PUBLIC_NEIGHBOURS = [
    "100.63.255.255",  # just below the /10
    "100.128.0.0",  # just above the /10
    "8.8.8.8",
]


@pytest.mark.parametrize("addr", CGNAT_LITERALS)
def test_cgnat_classified_internal(addr: str) -> None:
    assert _ip_is_internal(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize("addr", PUBLIC_NEIGHBOURS)
def test_cgnat_neighbours_not_over_blocked(addr: str) -> None:
    # Guard must be surgical: addresses just outside the /10 stay reachable.
    assert _ip_is_internal(ipaddress.ip_address(addr)) is False


@pytest.mark.parametrize("addr", CGNAT_LITERALS)
def test_cgnat_host_refused_by_ssrf_guard(addr: str) -> None:
    assert _ssrf_block_reason(addr) is not None
