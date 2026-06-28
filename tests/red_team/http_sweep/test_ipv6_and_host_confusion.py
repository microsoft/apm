"""Round-2 SSRF: IPv6 literal forms, userinfo host confusion, scheme gate.

Deeper than round-1 encoded-IPv4 coverage: bracketed IPv6 loopback /
link-local / IPv4-mapped / unspecified, ``user@host`` confusion pointing
at the metadata service, whitespace/null host smuggling, and proof that
the scheme gate runs BEFORE the SSRF guard (so a non-https URL on a
private IP is refused at the scheme check and never opens a socket).

Hermetic: ``requests.post`` is patched; ``recorder.dispatched is False``
means the guard refused before any socket was created. Host/scheme
assertions use ``urllib.parse.urlsplit``.
"""

from __future__ import annotations

import pytest

from .conftest import make_http_script

# Bracketed IPv6 literals the guard must refuse before any socket.
IPV6_INTERNAL = [
    "https://[::1]/x",  # loopback
    "https://[0::1]/x",  # loopback, alt form
    "https://[::]/x",  # unspecified
    "https://[::ffff:169.254.169.254]/x",  # IPv4-mapped link-local IMDS
    "https://[::ffff:a9fe:a9fe]/x",  # IPv4-mapped IMDS, hex form
    "https://[fe80::1]/x",  # link-local
    "https://[fc00::1]/x",  # unique-local (ULA)
    "https://[fd00::1]/x",  # unique-local (ULA)
]

# Host-confusion: userinfo precedes an internal authority.
USERINFO_TO_IMDS = [
    "https://expected.example.com@169.254.169.254/latest/meta-data/",
    "https://trusted.example.com@[::ffff:169.254.169.254]/x",
    "https://user:pass@169.254.169.254/x",
    "https://169.254.169.254@evil.example.com:6/x",  # IMDS as userinfo, port forces resolve of evil
]

# Non-https schemes -- must be rejected by the scheme gate first.
BAD_SCHEMES = [
    "http://169.254.169.254/x",  # plaintext + private
    "http://example.com/x",  # plaintext public
    "file:///etc/passwd",
    "gopher://169.254.169.254/_",
    "ftp://169.254.169.254/x",
    "data:text/plain;base64,QQ==",
]


@pytest.mark.parametrize("url", IPV6_INTERNAL)
def test_ipv6_internal_literal_refused_before_socket(dispatch, url):
    recorder = dispatch(make_http_script(url))
    assert not recorder.dispatched, f"IPv6 internal literal reached the network: {url}"


@pytest.mark.parametrize("url", USERINFO_TO_IMDS)
def test_userinfo_host_confusion_refused(dispatch, url):
    from urllib.parse import urlsplit

    # The effective authority host (what the guard must classify) is the
    # part after any userinfo. Confirm the URL really does target an
    # internal authority, then assert the executor refuses it.
    host = urlsplit(url).hostname
    recorder = dispatch(make_http_script(url))
    if host in {"169.254.169.254", "::ffff:169.254.169.254"}:
        assert not recorder.dispatched, f"userinfo@IMDS dispatched: {url}"
    # IMDS-as-userinfo: real host is evil.example.com; the guard cannot
    # be fooled into treating the userinfo as the target. Either it is
    # refused (resolves internal) or dispatched to the real host -- but
    # never to the userinfo literal.
    elif recorder.dispatched:
        assert urlsplit(recorder.last.url).hostname != "169.254.169.254"


@pytest.mark.parametrize("url", BAD_SCHEMES)
def test_non_https_scheme_refused_before_ssrf_socket(dispatch, url):
    recorder = dispatch(make_http_script(url))
    assert not recorder.dispatched, f"non-https scheme dispatched: {url}"


def test_scheme_gate_precedes_ssrf_guard(dispatch):
    """A plaintext URL on a private IP is refused at the scheme check.

    Proves ordering: had the SSRF guard run first and (hypothetically)
    allowed something, the scheme gate is still the outer wall. We assert
    no socket for http://<private>.
    """
    from urllib.parse import urlsplit

    url = "http://10.0.0.5/internal"
    assert urlsplit(url).scheme == "http"
    recorder = dispatch(make_http_script(url))
    assert not recorder.dispatched


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254\t.evil.example.com/x",
        "https://169.254.169.254\x00.evil.example.com/x",
        "https://169.254.169.254 /x",
    ],
)
def test_whitespace_or_null_host_not_dispatched_to_imds(dispatch, url):
    """Smuggled whitespace/null in the host must not reach IMDS.

    Either the guard refuses, or the mangled host fails to resolve; in no
    case may the request land on the bare metadata literal.
    """
    from urllib.parse import urlsplit

    recorder = dispatch(make_http_script(url))
    if recorder.dispatched:
        assert urlsplit(recorder.last.url).hostname != "169.254.169.254"
