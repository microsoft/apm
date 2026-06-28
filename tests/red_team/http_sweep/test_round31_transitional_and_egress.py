"""Round-31 red-team (HTTP / SSRF / network-bounds): transitional-IPv6 +
egress/resource proofs.

Adversarial hardening sweep round 31. Every probe drives the REAL
``apm_cli.core.script_executors`` dispatch surface -- ``_prepare_http``,
``_dispatch_http_request``, ``dispatch_http_batch``, the resolve-and-pin
``_ssrf_safe_connect``, the cached guarded/capturing ``requests`` sessions,
and ``_ip_is_internal`` / ``_ssrf_block_reason`` -- against REAL ``127.0.0.1``
listeners with a connection-count sentinel and a CONNECT-capturing proxy. No
dispatch or guard is reimplemented.

http held CLEAN r24-r30. Round-31 PIVOT (novel egress + resource vectors):

1. IPv4-MAPPED / TRANSITIONAL IPv6 (highest novelty): ``::ffff:169.254.169.254``,
   ``::ffff:7f00:1`` (loopback), NAT64 ``64:ff9b::a9fe:a9fe`` / ``::7f00:1``,
   6to4 ``2002::`` wrapping a private v4, deprecated IPv4-compatible
   ``::a9fe:a9fe``. ``_ip_is_internal`` must classify the EMBEDDED v4 (or the
   reserved/private wrapper) as internal; driven end-to-end so the metadata /
   loopback sentinel gets ZERO hits. RED-BEFORE: a naive stdlib-predicate-only
   classifier (no ipv4_mapped unwrap, no is_reserved) would let them through.
2. REDIRECT beyond a single 302: 307/308 to a loopback host, a relative-path
   Location, a non-http scheme Location (gopher/file). ``allow_redirects=False``
   makes all moot -- prove the redirector is hit but the internal sentinel is not.
3. PROXY edge: ``HTTPS_PROXY`` at a host that itself resolves to a private IP is
   LEGITIMATE corporate config -- confirm a public destination STILL tunnels its
   CONNECT, and that the DESTINATION gate (not the proxy address) blocks a
   metadata destination (no CONNECT for it).
4. RESOURCE: many sequential dispatches with assorted failure modes
   (connect-refused, RST, header-phase slow-loris) leave thread/fd/permit counts
   BOUNDED with N (no leak/growth).
5. SCHEME / URL parsing: userinfo@metadata, trailing-dot FQDN metadata,
   embedded CR/LF/NUL host (CONNECT request-smuggling), punycode host decoding
   to a metadata name.

Every URL/host assertion parses with ``urllib.parse`` (never substring) per the
repo test-convention rule.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo, ScriptEntry

from ._workers.servers import CaptureProxy, HeaderDribbleServer, SentinelServer

# Captured at import, BEFORE the http_sweep conftest autouse fixtures patch the
# module globals to None, so probes needing the production path get the real
# session builders / resolver (mirrors round-23 / round-26 / round-29 / round-30).
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_GET_GUARDED = se._get_guarded_session
_REAL_GET_CAPTURING = se._get_capturing_session


def _make_event() -> LifecycleEvent:
    return LifecycleEvent(
        event="post-install",
        packages=[PackageInfo(name="org/repo", reference="v1")],
        scope="project",
        timestamp="2026-01-01T00:00:00Z",
        cli_version="0.0.0",
        working_directory="/home/victim/project",
    )


def _http_script(url: str) -> ScriptEntry:
    return ScriptEntry(script_type="http", event="post-install", url=url)


def _fd_count() -> int:
    for path in ("/dev/fd", "/proc/self/fd"):
        with contextlib.suppress(OSError):
            return len(os.listdir(path))
    return -1


def _live_dispatch_threads() -> int:
    return sum(
        1
        for t in threading.enumerate()
        if t.is_alive() and (t.name == "apm-http-post" or "apm-http" in t.name)
    )


def _capture_logs(monkeypatch) -> list[dict[str, str]]:
    logs: list[dict[str, str]] = []

    def _fake(event, stype, url, stdout="", stderr="", status=""):
        logs.append({"status": status, "stdout": stdout, "stderr": stderr, "url": url})

    monkeypatch.setattr(se, "_append_to_script_log", _fake)
    return logs


@pytest.fixture
def _proxy_env():
    """Save/restore proxy env and reset the process-cached sessions."""
    saved = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")}
    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None
    try:
        yield
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        se._GUARDED_SESSION = None
        se._CAPTURING_SESSION = None


# ===========================================================================
# VECTOR 1 -- IPv4-mapped / transitional IPv6 family (highest novelty).
# ===========================================================================

# (literal, embedded/effective internal v4 it stands for)
_TRANSITIONAL = [
    ("::ffff:169.254.169.254", "link-local/metadata via IPv4-mapped"),
    ("::ffff:7f00:1", "loopback via IPv4-mapped (hex)"),
    ("64:ff9b::a9fe:a9fe", "NAT64 wrapping 169.254.169.254"),
    ("64:ff9b::7f00:1", "NAT64 wrapping 127.0.0.1"),
    ("2002:a9fe:a9fe::", "6to4 wrapping 169.254.169.254"),
    ("2002:c0a8:1::", "6to4 wrapping 192.168.0.1"),
    ("::a9fe:a9fe", "IPv4-compatible 169.254.169.254"),
    ("::7f00:1", "IPv4-compatible 127.0.0.1"),
]


@pytest.mark.parametrize("literal,desc", _TRANSITIONAL)
def test_transitional_ipv6_classified_internal(literal, desc):
    """Each transitional/mapped form is internal per the REAL _ip_is_internal.

    ``_ip_is_internal`` either unwraps the IPv4-mapped embed (::ffff:0:0/96) or
    relies on the wrapper prefix being is_reserved (NAT64, IPv4-compatible) /
    is_private (6to4) -- in every case the address an SSRF guard must refuse.
    """
    import ipaddress

    obj = ipaddress.ip_address(literal)
    assert se._ip_is_internal(obj) is True, f"{literal} ({desc}) must be internal"
    assert se._ssrf_block_reason(literal) == "internal address"


@pytest.mark.parametrize("literal,desc", _TRANSITIONAL)
def test_transitional_ipv6_refused_end_to_end_no_egress(literal, desc, _proxy_env, monkeypatch):
    """End-to-end through dispatch_http_batch + REAL guarded session: refused.

    A bracketed transitional-IPv6 URL must be refused at ``_prepare_http`` so no
    ``requests.post`` ever runs and no socket opens. We assert via a recorder
    that the egress layer is never reached.
    """
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    monkeypatch.setattr(se, "_get_guarded_session", _REAL_GET_GUARDED)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    _capture_logs(monkeypatch)

    posted: list[str] = []
    real_dispatch = se._dispatch_http_request

    def _spy(url, *a, **k):
        posted.append(url)
        return real_dispatch(url, *a, **k)

    monkeypatch.setattr(se, "_dispatch_http_request", _spy)

    url = f"https://[{literal}]/x"
    # prepare refuses before any egress
    assert se._prepare_http(_http_script(url), _make_event()) is None
    # batch seam: worker calls _prepare_http -> None -> never dispatches
    workers = se.dispatch_http_batch([_http_script(url)], _make_event())
    for w in workers:
        w.join(timeout=5)
    assert posted == [], f"{literal} ({desc}) reached the egress layer"


@pytest.mark.parametrize("literal,desc", _TRANSITIONAL)
def test_transitional_ipv6_red_before_naive_classifier_flips(literal, desc, monkeypatch):
    """RED-BEFORE: a naive stdlib-only classifier would ADMIT these.

    Replace ``_ip_is_internal`` with a version that drops the ipv4_mapped unwrap
    AND the is_reserved/is_private wrapper checks (only loopback/link-local on the
    raw v6 object). At least the NAT64 / IPv4-compatible / mapped forms then flip
    to ALLOWED -- proving the production guard's extra checks are load-bearing.
    """
    import ipaddress

    def _naive(ip):
        # Deliberately weak: no mapped unwrap, no is_reserved/is_private.
        return bool(ip.is_loopback or ip.is_link_local or ip.is_multicast)

    monkeypatch.setattr(se, "_ip_is_internal", _naive)
    obj = ipaddress.ip_address(literal)
    # The naive classifier admits the embedded-internal transitional forms that
    # rely on mapped-unwrap or reserved/private wrapper classification.
    naive_internal = _naive(obj)
    # Not every form flips (raw ::ffff: link-local v6 may still be link_local),
    # but the NAT64 / IPv4-compatible / 6to4 forms do -- assert the suite as a
    # whole demonstrates a flip for the reserved/private-wrapped family.
    if literal.startswith(("64:ff9b", "::a9fe", "::7f00", "2002:")):
        assert naive_internal is False, f"naive classifier should ADMIT {literal}"
        # and the REAL classifier refuses it (restored after monkeypatch undo)
    # sanity: production verdict is internal regardless
    monkeypatch.undo()
    assert se._ip_is_internal(obj) is True


def test_transitional_nat64_loopback_pin_refuses_real_sentinel(_proxy_env, monkeypatch):
    """The connect-time pin refuses a host RESOLVING to a NAT64-loopback record.

    A hostname answers a NAT64 address wrapping 127.0.0.1. ``_ssrf_safe_connect``
    re-resolves through the SAME getaddrinfo and must raise ``_SSRFConnectError``;
    the real loopback sentinel receives ZERO connections.
    """
    with SentinelServer() as sentinel:

        def _resolver(host, *a, **k):
            if host == "nat64-rebind.evil.test":
                # NAT64 wrapping loopback -- IPv6 record; internal per guard.
                return [
                    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::7f00:1", 0, 0, 0)),
                ]
            return _REAL_GETADDRINFO(host, *a, **k)

        monkeypatch.setattr(socket, "getaddrinfo", _resolver)
        # up-front gate refuses
        assert se._ssrf_block_reason("nat64-rebind.evil.test") == "resolves to internal address"
        # connect-time pin refuses too (defense in depth)
        with pytest.raises(se._SSRFConnectError):
            se._ssrf_safe_connect(("nat64-rebind.evil.test", sentinel.port), timeout=1.0)
        assert not sentinel.connected.wait(0.5)
        assert sentinel.hits == 0


# ===========================================================================
# VECTOR 2 -- redirect semantics beyond a single 302.
# ===========================================================================
def _redirect_bytes(status: bytes, location: str) -> bytes:
    return (
        b"HTTP/1.1 " + status + b"\r\n"
        b"Location: " + location.encode("latin1") + b"\r\n"
        b"Content-Length: 0\r\n"
        b"Connection: close\r\n\r\n"
    )


@pytest.mark.parametrize(
    "status",
    [b"307 Temporary Redirect", b"308 Permanent Redirect", b"301 Moved Permanently"],
)
def test_redirect_307_308_to_loopback_not_followed(status, _proxy_env, monkeypatch):
    """3xx (incl. 307/308) Location at a loopback sentinel is NEVER dialed."""
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    logs = _capture_logs(monkeypatch)

    with SentinelServer() as sentinel:
        location = f"http://127.0.0.1:{sentinel.port}/internal"
        with SentinelServer(response=_redirect_bytes(status, location)) as redirector:
            url = f"http://127.0.0.1:{redirector.port}/start"
            se._dispatch_http_request(
                url, "{}", {"Content-Type": "application/json"}, 4.0, "post-install", url
            )
            assert redirector.connected.wait(3.0)
            assert not sentinel.connected.wait(0.8)
            assert sentinel.hits == 0
    assert logs, "dispatch must log a terminal (un-followed) outcome"


def test_redirect_relative_path_not_followed(_proxy_env, monkeypatch):
    """A relative-path Location (no host) is not resolved/followed either."""
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    logs = _capture_logs(monkeypatch)

    with SentinelServer(response=_redirect_bytes(b"302 Found", "/internal/meta")) as redirector:
        url = f"http://127.0.0.1:{redirector.port}/start"
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 4.0, "post-install", url
        )
        assert redirector.connected.wait(3.0)
        # only ONE connection ever (the relative redirect is not re-fetched)
        time.sleep(0.4)
        assert redirector.hits == 1
    assert logs


@pytest.mark.parametrize(
    "scheme_loc",
    [
        "gopher://127.0.0.1:11211/_internal",
        "file:///etc/passwd",
        "ftp://169.254.169.254/latest",
    ],
)
def test_redirect_nonhttp_scheme_location_not_followed(scheme_loc, _proxy_env, monkeypatch):
    """A non-http(s) scheme Location is never followed (allow_redirects=False)."""
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    logs = _capture_logs(monkeypatch)

    with SentinelServer(response=_redirect_bytes(b"302 Found", scheme_loc)) as redirector:
        url = f"http://127.0.0.1:{redirector.port}/start"
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 4.0, "post-install", url
        )
        assert redirector.connected.wait(3.0)
        time.sleep(0.3)
        assert redirector.hits == 1
    assert logs


# ===========================================================================
# VECTOR 3 -- proxy at a private IP is legit; destination gate is what blocks.
# ===========================================================================
def test_private_ip_proxy_tunnels_public_and_blocks_metadata_destination(_proxy_env, monkeypatch):
    """A loopback/private-IP proxy is a LEGITIMATE corporate proxy.

    (a) NON-NEGOTIABLE corporate egress: a public destination still tunnels
        ``CONNECT host:443`` through the private-IP proxy.
    (b) A metadata DESTINATION is refused up-front by ``_prepare_http`` and emits
        NO CONNECT through the same proxy -- the destination gate, not the proxy
        address, is the boundary.
    """
    with CaptureProxy(stall=True) as proxy:
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.port}"  # private-IP proxy
        os.environ.pop("NO_PROXY", None)
        monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)

        def _resolver(host, *a, **k):
            if host == "telemetry.corp.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
            if host == "metadata.evil.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
            return _REAL_GETADDRINFO(host, *a, **k)

        monkeypatch.setattr(socket, "getaddrinfo", _resolver)

        # (b) metadata destination refused before any egress
        bad_url = "https://metadata.evil.test/latest/meta-data/"
        assert se._prepare_http(_http_script(bad_url), _make_event()) is None
        assert not proxy.connected.wait(0.5)
        assert proxy.first_line is None

        # (a) public destination tunnels through the private-IP proxy
        good_url = "https://telemetry.corp.test/ingest"
        se._dispatch_http_request(
            good_url, "{}", {"Content-Type": "application/json"}, 3.0, "post-install", good_url
        )
        assert proxy.connected.wait(4.0)
        assert proxy.first_line is not None
        method, target, _proto = proxy.first_line.split(" ", 2)
        assert method == "CONNECT"
        host, _, port = target.partition(":")
        assert host == urlsplit(good_url).hostname
        assert port == "443"


# ===========================================================================
# VECTOR 4 -- sequential failure modes leave bounded threads/fds/permits.
# ===========================================================================
def test_sequential_connect_refused_bounded(_proxy_env, monkeypatch):
    """N sequential connect-refused dispatches: no thread/fd/permit growth."""
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    _capture_logs(monkeypatch)

    from apm_cli.core.script_executors import _HTTP_INFLIGHT

    # a closed port -> connection refused
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    refused_port = s.getsockname()[1]
    s.close()

    base_threads = _live_dispatch_threads()
    base_fd = _fd_count()
    for _ in range(25):
        url = f"http://127.0.0.1:{refused_port}/x"
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 2.0, "post-install", url
        )
    time.sleep(0.5)
    assert _live_dispatch_threads() <= base_threads + 2
    if base_fd != -1:
        assert _fd_count() <= base_fd + 8
    # all permits reclaimed: we can acquire the full cap then release it
    acquired = 0
    for _ in range(se.MAX_HTTP_DISPATCH_THREADS):
        if _HTTP_INFLIGHT.acquire(blocking=False):
            acquired += 1
        else:
            break
    for _ in range(acquired):
        _HTTP_INFLIGHT.release()
    assert acquired == se.MAX_HTTP_DISPATCH_THREADS, "permits leaked after refused dispatches"


def test_sequential_rst_midhandshake_bounded(_proxy_env, monkeypatch):
    """N sequential RST-on-connect dispatches stay bounded and reclaim permits."""
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    _capture_logs(monkeypatch)
    from apm_cli.core.script_executors import _HTTP_INFLIGHT

    base_threads = _live_dispatch_threads()
    with SentinelServer(reset=True) as rstserver:
        for _ in range(20):
            url = f"http://127.0.0.1:{rstserver.port}/x"
            se._dispatch_http_request(
                url, "{}", {"Content-Type": "application/json"}, 2.0, "post-install", url
            )
    time.sleep(0.6)
    assert _live_dispatch_threads() <= base_threads + 2
    acquired = 0
    for _ in range(se.MAX_HTTP_DISPATCH_THREADS):
        if _HTTP_INFLIGHT.acquire(blocking=False):
            acquired += 1
        else:
            break
    for _ in range(acquired):
        _HTTP_INFLIGHT.release()
    assert acquired == se.MAX_HTTP_DISPATCH_THREADS, "permits leaked after RST dispatches"


def test_header_phase_slowloris_forceclosed_and_permit_reclaimed(_proxy_env, monkeypatch):
    """Header-phase slow-loris (status line never completes) is force-closed.

    Under stream=True the dispatcher reads the status line; a server dribbling
    one header byte forever past the total deadline must be force-closed and its
    permit reclaimed -- N such dispatches do not pin the semaphore.
    """
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    logs = _capture_logs(monkeypatch)
    from apm_cli.core.script_executors import _HTTP_INFLIGHT

    base_threads = _live_dispatch_threads()
    with HeaderDribbleServer(hold=10.0) as drib:
        for _ in range(5):
            url = f"http://127.0.0.1:{drib.port}/x"
            t0 = time.monotonic()
            se._dispatch_http_request(
                url, "{}", {"Content-Type": "application/json"}, 1.5, "post-install", url
            )
            # the dispatcher must return within ~ deadline + abandon grace, not hang
            assert time.monotonic() - t0 < 8.0
    time.sleep(1.0)
    assert _live_dispatch_threads() <= base_threads + 2
    acquired = 0
    for _ in range(se.MAX_HTTP_DISPATCH_THREADS):
        if _HTTP_INFLIGHT.acquire(blocking=False):
            acquired += 1
        else:
            break
    for _ in range(acquired):
        _HTTP_INFLIGHT.release()
    assert acquired == se.MAX_HTTP_DISPATCH_THREADS, "permits leaked after slow-loris"
    assert any(log["status"] == "error" for log in logs)


# ===========================================================================
# VECTOR 5 -- scheme / URL parsing edge cases.
# ===========================================================================
def test_userinfo_at_metadata_refused():
    """A userinfo@host URL pointing at metadata is classified by HOST, refused."""
    url = "https://user:pass@169.254.169.254/latest/meta-data/"
    assert urlsplit(url).hostname == "169.254.169.254"
    assert se._prepare_http(_http_script(url), _make_event()) is None


def test_trailing_dot_fqdn_metadata_refused():
    """A trailing-dot absolute metadata FQDN is refused (rstrip('.') normalised)."""
    url = "https://metadata.google.internal./computeMetadata/v1/"
    assert se._prepare_http(_http_script(url), _make_event()) is None
    # and the literal trailing-dot metadata IP too
    assert se._prepare_http(_http_script("https://169.254.169.254./x"), _make_event()) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://foo\r\nHost: 169.254.169.254/x",  # CRLF in authority
        "https://169.254.169.254\x00.example.com/x",  # NUL in host
        "https://169.254.169.254\r\n.evil/x",
    ],
)
def test_crlf_nul_host_no_smuggled_egress(url, _proxy_env, monkeypatch):
    """A CR/LF/NUL-bearing host must not smuggle a CONNECT to metadata.

    Whether the guard refuses up-front OR the request layer rejects the control
    chars, the REAL outcome must be: the CONNECT-capturing proxy receives ZERO
    bytes mentioning the metadata target. Driven through the real proxy path.
    """
    with CaptureProxy(stall=True) as proxy:
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.port}"
        os.environ.pop("NO_PROXY", None)
        monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
        monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
        _capture_logs(monkeypatch)

        prepared = se._prepare_http(_http_script(url), _make_event())
        if prepared is not None:
            # not refused up-front -> drive the egress layer; it must fail-closed
            real_url = prepared[0]
            se._dispatch_http_request(
                real_url, "{}", {"Content-Type": "application/json"}, 2.0, "post-install", real_url
            )
        time.sleep(0.5)
        # No CONNECT carrying the metadata host ever reached the proxy.
        line = proxy.first_line or ""
        assert "169.254.169.254" not in line
        assert proxy.first_line is None or not line.startswith("CONNECT 169.254")


def test_punycode_host_decoding_to_metadata_refused(_proxy_env, monkeypatch):
    """A punycode/IDNA host that resolves to an internal address is refused.

    Model a homograph host whose A record is the metadata IP. ``_ssrf_block_reason``
    resolves and classifies the RESOLVED address -> refused; no egress.
    """
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)

    def _resolver(host, *a, **k):
        if host == "xn--metadata-evil.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
        return _REAL_GETADDRINFO(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", _resolver)
    url = "https://xn--metadata-evil.test/latest"
    assert se._ssrf_block_reason("xn--metadata-evil.test") == "resolves to internal address"
    assert se._prepare_http(_http_script(url), _make_event()) is None
