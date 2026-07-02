"""SSRF bypass via non-dotted-decimal IP encodings and hostname tricks.

Finding ``d3-ssrf-ip-encoding``. Even a naive SSRF guard that only does
``ipaddress.ip_address(parsed.hostname)`` and checks ``.is_private`` is
defeated by these encodings, because ``urlparse`` returns the raw
hostname string (e.g. ``"2130706433"`` or ``"127.0.0.1."``) which
``ipaddress.ip_address`` rejects as not-an-IP -- so the guard would fall
through to "allow" while the OS resolver still routes the request to
127.0.0.1.

On HEAD there is no guard at all, so all of these dispatch. The tests
assert the SECURE expectation (refused) and therefore fail on head. They
double as a design note: the guard must canonicalise the host (parse
int/hex/octal forms and trailing dots) BEFORE classifying, or resolve
via the OS and inspect the resolved address.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import pytest

from .conftest import make_http_script

# (label, url, canonical-loopback-or-private-equivalent)
# Hostnames that ipaddress.ip_address() CANNOT parse as a literal -- these
# defeat a naive ipaddress-only guard.
NON_LITERAL_TARGETS = [
    ("decimal-ipv4", "https://2130706433/", "127.0.0.1"),
    ("hex-ipv4", "https://0x7f000001/", "127.0.0.1"),
    ("trailing-dot-loopback", "https://127.0.0.1./", "127.0.0.1"),
    ("trailing-dot-rfc1918", "https://10.0.0.5./", "10.0.0.5"),
]

# Full set a hardened executor must refuse (adds an IPv4-mapped IPv6 form
# whose literal IS parseable but whose mapped address is loopback).
ENCODED_TARGETS = [
    *NON_LITERAL_TARGETS,
    ("ipv4-mapped-ipv6", "https://[::ffff:127.0.0.1]/", "127.0.0.1"),
]


class TestEncodedSsrfBypass:
    @pytest.mark.parametrize(
        "url",
        [t[1] for t in ENCODED_TARGETS],
        ids=[t[0] for t in ENCODED_TARGETS],
    )
    def test_encoded_internal_destination_refused(self, dispatch, url: str) -> None:
        recorder = dispatch(make_http_script(url))
        assert not recorder.dispatched, (
            f"SSRF: executor dispatched to encoded internal destination {url!r}; "
            "a guard must canonicalise the host before classifying"
        )

    def test_naive_ipaddress_guard_would_be_bypassed(self) -> None:
        """Documents WHY substring/ipaddress-only guards fail.

        The raw hostname from these URLs is not parseable as an IP literal,
        so ``ipaddress.ip_address(hostname)`` raises -- a naive guard that
        treats "not an IP literal" as "public/allow" is bypassed.
        """
        for _label, url, canonical in NON_LITERAL_TARGETS:
            host = urlparse(url).hostname or ""
            with pytest.raises(ValueError):
                ipaddress.ip_address(host)
            # ...yet the canonical address these route to is private/loopback.
            assert (
                ipaddress.ip_address(canonical).is_loopback
                or ipaddress.ip_address(canonical).is_private
            )
