"""r4-http-1: RFC 3879 deprecated IPv6 site-local (fec0::/10) is internal.

The stdlib ``ipaddress`` predicates report site-local space as is_global=True /
is_private=False -- only fe80::/10 link-local is flagged -- so an SSRF guard
built on the stdlib predicates would dispatch a request to a fec0:: target.
This is the same blind-spot class as the CGNAT (100.64.0.0/10) gap: the guard
must treat the whole /10 as internal and refuse a literal in that range before
any socket, while staying surgical (link-local sibling febf:: and public IPv6
must not be over-blocked).
"""

from __future__ import annotations

import ipaddress

import pytest

from apm_cli.core.script_executors import _ip_is_internal, _ssrf_block_reason

SITE_LOCAL_LITERALS = [
    "fec0::",  # network address
    "fec0::1",
    "fed0::abcd",  # mid-range
    "feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",  # last in /10
]

NON_SITE_LOCAL = [
    "febf::1",  # just below fec0::/10 (link-local /10, already blocked separately)
    "2606:4700::1",  # public IPv6 (Cloudflare) must stay reachable
    "2001:4860:4860::8888",  # public IPv6 (Google DNS)
]


@pytest.mark.parametrize("addr", SITE_LOCAL_LITERALS)
def test_site_local_classified_internal(addr: str) -> None:
    assert _ip_is_internal(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize("addr", NON_SITE_LOCAL)
def test_public_ipv6_not_over_blocked(addr: str) -> None:
    # febf:: is link-local (blocked by is_link_local, not the site-local clause);
    # the two public addresses must classify as reachable.
    if addr == "febf::1":
        assert _ip_is_internal(ipaddress.ip_address(addr)) is True
    else:
        assert _ip_is_internal(ipaddress.ip_address(addr)) is False


@pytest.mark.parametrize("addr", SITE_LOCAL_LITERALS)
def test_site_local_host_refused_by_ssrf_guard(addr: str) -> None:
    assert _ssrf_block_reason(addr) is not None
