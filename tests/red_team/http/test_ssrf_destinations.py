"""SSRF: the HTTP executor dispatches to internal/sensitive destinations.

Finding family ``d3-ssrf-*``. ``_execute_http`` enforces HTTPS-only but
has NO guard against private / loopback / link-local / unique-local /
known-metadata destinations. Every test in :class:`TestSsrfBlocked`
asserts the SECURE expectation -- the executor must REFUSE to dispatch
to these destinations -- and therefore FAILS on head code (genuine
break). :class:`TestPublicAllowed` is the regression trap: a real SSRF
guard must keep dispatching to public hosts.

Hermetic: ``requests.post`` is patched (see conftest). We assert on
whether a call was recorded, never on real network behaviour.

Expected guard shape (informs the fix, not asserted here): resolve the
URL hostname and reject if ANY resolved address is in a private,
loopback, link-local, or unique-local range, plus a hostname blocklist
for cloud metadata endpoints (e.g. ``metadata.google.internal``).
DNS-rebinding residual risk: a guard that resolves once then lets
``requests`` resolve again is TOCTOU-vulnerable; the secure design pins
the validated IP (custom adapter / resolved-IP connect).
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from .conftest import make_http_script

# (label, url) -- destinations a hardened executor must refuse.
INTERNAL_TARGETS = [
    ("aws-imds-link-local", "https://169.254.169.254/latest/meta-data/iam/"),
    ("ipv4-loopback", "https://127.0.0.1/admin"),
    ("ipv4-loopback-alt", "https://127.0.0.53/"),
    ("ipv6-loopback", "https://[::1]/"),
    ("localhost-name", "https://localhost/internal"),
    ("rfc1918-10", "https://10.1.2.3/"),
    ("rfc1918-192-168", "https://192.168.1.1/"),
    ("rfc1918-172-16", "https://172.16.0.1/"),
    ("ula-ipv6", "https://[fd00::1]/"),
    ("link-local-ipv6", "https://[fe80::1]/"),
    ("gcp-metadata-hostname", "https://metadata.google.internal/computeMetadata/v1/"),
]


class TestSsrfBlocked:
    """Each case is a genuine break on head: dispatch currently happens."""

    @pytest.mark.parametrize(
        "url",
        [t[1] for t in INTERNAL_TARGETS],
        ids=[t[0] for t in INTERNAL_TARGETS],
    )
    def test_internal_destination_is_not_dispatched(self, dispatch, url: str) -> None:
        recorder = dispatch(make_http_script(url))
        # SECURE expectation: an SSRF guard refuses these outright.
        # HEAD: no guard exists, so the request IS dispatched -> this fails.
        assert not recorder.dispatched, (
            f"SSRF: executor dispatched to internal destination "
            f"{urlparse(url).hostname!r}; expected it to be refused"
        )

    def test_metadata_link_local_exact_target(self, dispatch) -> None:
        """Highest-value target: cloud IMDS over the link-local address."""
        url = "https://169.254.169.254/latest/meta-data/iam/security-credentials/"
        recorder = dispatch(make_http_script(url))
        assert not recorder.dispatched, (
            "SSRF: executor dispatched to the cloud metadata link-local "
            "address (169.254.169.254); a guard must refuse 169.254.0.0/16"
        )


class TestPublicAllowed:
    """Regression trap: a guard must NOT break public-host delivery."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://analytics.example.com/events",
            "https://api.github.com/v0/telemetry",
            "https://hooks.corp.net/apm",
        ],
    )
    def test_public_destination_is_dispatched(self, dispatch, url: str) -> None:
        recorder = dispatch(make_http_script(url))
        assert recorder.dispatched, "public destination must still be delivered"
        # The captured URL must be exactly the configured target (parsed).
        sent = urlparse(recorder.last.url)
        want = urlparse(url)
        assert (sent.scheme, sent.hostname, sent.path) == (
            want.scheme,
            want.hostname,
            want.path,
        )
