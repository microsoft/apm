"""Round-2 SSRF: DNS-rebinding TOCTOU on the SSRF guard.

The guard (:func:`_ssrf_block_reason`) resolves a DNS *name* through
``socket.getaddrinfo`` and classifies the result. But the dispatch layer
hands the original URL (still carrying the *hostname*) to
``requests.post``, which re-resolves the name independently at socket
connect time. The guard does NOT resolve-and-pin: it never substitutes
the validated IP into the connection. That leaves a classic DNS-rebinding
window -- an attacker-controlled domain that answers a public A record on
the guard's lookup and ``169.254.169.254`` on the request's lookup reaches
the metadata service despite passing the guard.

These tests are hermetic: ``socket.getaddrinfo`` and ``requests.post`` are
both patched, so nothing is resolved or connected for real. Host
assertions use ``urllib.parse.urlsplit`` (never substring).
"""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import urlsplit

from .conftest import make_http_script

REBIND_HOST = "rebind.attacker.test"
PUBLIC_IP = "93.184.216.34"  # guard sees this -> allowed
PRIVATE_IP = "169.254.169.254"  # request would re-resolve to this


def _addrinfo(ip: str):
    import socket

    family = socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


def test_guard_resolves_name_but_request_gets_unpinned_hostname(dispatch):
    """Guard validates a public IP, yet requests.post receives the raw name.

    This is the rebinding window: the connection target is the hostname,
    not the IP the guard approved, so a second DNS answer can point at the
    metadata service.
    """
    calls: list = []

    def _fake_getaddrinfo(host, *a, **k):
        calls.append(host)
        return _addrinfo(PUBLIC_IP)

    url = f"https://{REBIND_HOST}/collect"
    with patch(
        "apm_cli.core.script_executors.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo,
    ):
        recorder = dispatch(make_http_script(url))

    # The guard DID resolve the name (so it had the IP available to pin).
    assert REBIND_HOST in calls, "guard must have resolved the hostname"
    # The request was dispatched...
    assert recorder.dispatched, "guard approved the public-resolving name"
    # ...with the UNPINNED hostname. urlsplit proves the connect target is
    # the name, not the validated literal IP -> rebinding is possible.
    target = urlsplit(recorder.last.url)
    assert target.hostname == REBIND_HOST
    # The validated public IP was never substituted into the URL.
    assert target.hostname != PUBLIC_IP


def test_guard_does_not_revalidate_at_connect_time(dispatch):
    """No second SSRF classification happens between approval and connect.

    Model rebinding directly: getaddrinfo returns public on the guard's
    single lookup; there is no hook that re-checks the address requests
    actually connects to. We assert the executor performs exactly one
    guard-side resolution and then trusts the name forever.
    """
    lookups: list = []

    def _fake_getaddrinfo(host, *a, **k):
        lookups.append(host)
        # First (guard) answer benign; any later answer would be IMDS, but
        # the executor never asks again -- proving the absent pin.
        return _addrinfo(PUBLIC_IP if len(lookups) == 1 else PRIVATE_IP)

    url = f"https://{REBIND_HOST}/x"
    with patch(
        "apm_cli.core.script_executors.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo,
    ):
        recorder = dispatch(make_http_script(url))

    # Exactly one guard-side resolution; the request layer's own re-resolve
    # is outside the executor and unguarded.
    assert lookups == [REBIND_HOST]
    assert recorder.dispatched
    assert urlsplit(recorder.last.url).hostname == REBIND_HOST
