"""Round-6 (r6-http-1) trap: backslash-authority parser confusion in SSRF guard.

``urllib.parse.urlparse`` and the connection-layer parser
(``urllib3.util.parse_url``) disagree on where the authority ends when a
backslash appears in the host. A URL like ``https://169.254.169.254\\.evil/``
parses (in ``urlparse``) with hostname ``169.254.169.254\\.evil`` (treated as a
public name), but the HTTP client's own URL splitter can read the connect host
as the bare internal literal ``169.254.169.254`` -- so the SSRF hostname check,
run only on ``urlparse``'s view, waves it through while the socket dials the
metadata endpoint. This only bit the FALLBACK path (no DNS-pinned session);
the pinned path already re-resolves.

The fix cross-checks the connection-layer host against ``urlparse``'s hostname
(normalizing IPv6 brackets + case) and runs the SSRF reason on BOTH views,
rejecting on any mismatch. Legit IPv6 / mixed-case URLs must NOT be
false-rejected by the normalization.
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import LifecycleEvent, ScriptEntry

_EVENT = LifecycleEvent(event="post-install")


def _http_entry(url: str) -> ScriptEntry:
    return ScriptEntry(script_type="http", event="post-install", url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254\\.evil.example/latest/meta-data/",
        "https://169.254.169.254:80\\@evil.example/x",
        "https://127.0.0.1\\@evil.example/x",
        "https://[::1]\\.evil.example/x",
    ],
)
def test_backslash_authority_is_refused(url: str) -> None:
    """The parser-confusion URLs must all fail closed (prepare returns None)."""
    assert se._prepare_http(_http_entry(url), _EVENT) is None, url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/ingest",
        "https://API.Example.COM/x",  # mixed case must survive normalization
        "https://[2606:4700:4700::1111]/x",  # public IPv6, bracketed
        "https://[2001:4860:4860::8888]:8443/x",  # public IPv6 with port
    ],
)
def test_legit_urls_not_false_rejected(url: str) -> None:
    """Normalization must not break ordinary or IPv6 / mixed-case hosts."""
    assert se._prepare_http(_http_entry(url), _EVENT) is not None, url


def test_internal_ipv6_literal_still_blocked() -> None:
    assert se._prepare_http(_http_entry("https://[::1]/x"), _EVENT) is None


def test_connect_layer_host_normalizes_brackets_and_case() -> None:
    """_connect_layer_host + _normalize_host agree with urlparse for legit IPv6."""
    host = se._connect_layer_host("https://[2606:4700:4700::1111]:443/x")
    assert host is not None
    assert se._normalize_host(host) == se._normalize_host("2606:4700:4700::1111")
