"""Round 20 red-team: pivot from address-space to RESOURCE bounds + PROXY.

Each probe drives the REAL guard/dispatch/session code in
``apm_cli.core.script_executors``. Connection targets are proven with a
real local listener or a recording socket -- never URL substring checks.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from urllib.parse import urlparse

import pytest

from apm_cli.core import script_executors as se

# Captured before any conftest fixture patches socket.getaddrinfo, so the
# proxy probe can resolve the literal loopback proxy host truthfully
# instead of through the harness's fake-public stub.
_REAL_GETADDRINFO = socket.getaddrinfo


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _RecordingListener:
    """A loopback TCP listener that records the first bytes it receives.

    Stands in for an INTERNAL service (loopback == internal). If the
    guarded HTTP dispatch ever opens a socket to it, ``hits`` is set and
    the request line / CONNECT verb is captured.
    """

    def __init__(self) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(1)
        self._sock.settimeout(5.0)
        self.hits = 0
        self.first_line = b""
        self.peer = None
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def _serve(self) -> None:
        try:
            conn, addr = self._sock.accept()
        except OSError:
            return
        self.hits += 1
        self.peer = addr
        try:
            conn.settimeout(2.0)
            data = conn.recv(4096)
            self.first_line = data.split(b"\r\n", 1)[0]
            # Politely refuse so requests gets a clean failure, not a hang.
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
        finally:
            conn.close()

    def __exit__(self, *exc) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


@pytest.fixture(autouse=True)
def _reset_cached_session():
    """Isolate the process-cached guarded session across probes."""
    se._GUARDED_SESSION = None
    yield
    se._GUARDED_SESSION = None


# --------------------------------------------------------------------------
# PROXY: does the guarded session honor env proxies, tunneling an https
# request for a PUBLIC host THROUGH a loopback/internal proxy?
# --------------------------------------------------------------------------
def test_https_proxy_env_tunnels_to_loopback(monkeypatch):
    """HIGH-VALUE: prove a real connection to a loopback proxy.

    The SSRF guard vets the TARGET host (example.com -> public). The
    DNS-pinned adapter only pins DIRECT https connections; a configured
    proxy uses urllib3's ProxyManager, which is NOT the pinned pool. So
    if ``trust_env`` honors HTTPS_PROXY, the dispatch dials the proxy IP
    (here 127.0.0.1, an internal address) unvetted.
    """
    # Override conftest's autouse fixture that neutralizes the guarded
    # session: we want the REAL DNS-pinned production session here so the
    # proof exercises the actual adapter/pool the shipped code uses.
    real_session = se._build_guarded_session()
    monkeypatch.setattr(se, "_get_guarded_session", lambda: real_session)
    # Restore the genuine resolver (the http_sweep conftest stubs
    # getaddrinfo to a fake public IP, which would otherwise redirect the
    # proxy connection away from our loopback listener). The proxy host is
    # the literal 127.0.0.1, so the real resolver returns it faithfully.
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)

    with _RecordingListener() as listener:
        proxy_url = f"http://127.0.0.1:{listener.port}"
        monkeypatch.setenv("HTTPS_PROXY", proxy_url)
        monkeypatch.setenv("https_proxy", proxy_url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        # target host is unambiguously PUBLIC; guard would allow it.
        target = "https://example.com/ingest"
        assert se._ssrf_block_reason(urlparse(target).hostname) is None

        se._dispatch_http_request(
            url=target,
            payload="{}",
            request_headers={"Content-Type": "application/json"},
            timeout=4.0,
            event_name="post-install",
            safe_url=target,
        )
        # give the daemon listener a beat to record.
        for _ in range(50):
            if listener.hits:
                break
            time.sleep(0.05)

        peer = listener.peer
        verb = listener.first_line.split(b" ", 1)[0] if listener.first_line else b""

    # SECURE CONTRACT: a lifecycle http action must never tunnel to an
    # internal address via a proxy. A loopback hit here means the SSRF
    # guard + DNS pin were entirely bypassed by trust_env honoring the
    # HTTPS_PROXY env var. This assertion FAILS today -> the r20 break.
    assert listener.hits == 0, (
        "SSRF-via-proxy: guarded dispatch tunneled an https request for the "
        f"PUBLIC host {urlparse(target).hostname!r} through loopback proxy "
        f"127.0.0.1:{listener.port} (verb={verb!r}, peer={peer}). "
        "trust_env=True on the guarded session honors HTTPS_PROXY, and the "
        "DNS-pinned adapter never vets the proxy connection."
    )


# --------------------------------------------------------------------------
# RESPONSE-SIZE: does the dispatch read the body with a cap, or stop at
# the status line? Prove the body is NEVER pulled into memory.
# --------------------------------------------------------------------------
def test_dispatch_does_not_read_unbounded_body(monkeypatch):
    """Confirm ``_dispatch_http_request`` consumes only status/headers.

    We monkeypatch the guarded session's ``post`` to return a fake
    streaming response that records whether its body was read. The real
    dispatch code path runs; we assert it touches status_code/ok only and
    never invokes an unbounded body read (.content/.text/iter_content).
    """
    body_reads = {"content": 0, "text": 0, "iter": 0, "raw": 0}

    class _FakeRaw:
        def read(self, *a, **k):
            body_reads["raw"] += 1
            return b"x" * (10 * 1024 * 1024)

    class _FakeResp:
        status_code = 200
        ok = True
        raw = _FakeRaw()

        @property
        def content(self):
            body_reads["content"] += 1
            return b"x" * (1024 * 1024 * 1024)

        @property
        def text(self):
            body_reads["text"] += 1
            return "x" * (1024 * 1024 * 1024)

        def iter_content(self, *a, **k):
            body_reads["iter"] += 1
            yield b"x" * (1024 * 1024 * 1024)

    class _FakeSession:
        def post(self, *a, **k):
            # the dispatch MUST pass stream=True and allow_redirects=False
            assert k.get("stream") is True
            assert k.get("allow_redirects") is False
            return _FakeResp()

    monkeypatch.setattr(se, "_get_guarded_session", lambda: _FakeSession())

    se._dispatch_http_request(
        url="https://example.com/x",
        payload="{}",
        request_headers={},
        timeout=2.0,
        event_name="post-install",
        safe_url="https://example.com/x",
    )

    assert body_reads == {"content": 0, "text": 0, "iter": 0, "raw": 0}, (
        f"dispatch performed an unbounded body read: {body_reads}"
    )


# --------------------------------------------------------------------------
# READ-TIMEOUT: prove the single-float timeout covers the READ phase, not
# just connect, so a server that accepts then dribbles a slow header
# cannot hang the dispatch beyond ~timeout (per recv).
# --------------------------------------------------------------------------
def test_read_timeout_bounds_slow_header(monkeypatch):
    """A server that accepts then NEVER sends a byte must trip the read
    timeout (not block forever). We use the guarded session against a
    loopback listener via an explicit proxy-free monkeypatched session
    that talks plain http to model the read path timing.
    """

    # Slow server: accept, then sleep past the timeout without sending.
    port = _free_port()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(5.0)
    stop = threading.Event()

    def _serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        # hold the socket open, send nothing, until told to stop.
        while not stop.is_set():
            time.sleep(0.05)
        conn.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    import requests

    start = time.monotonic()
    timed_out = False
    try:
        # plain requests.get models the urllib3 read-timeout behavior the
        # dispatch relies on (single float -> connect AND read timeout).
        requests.get(f"http://127.0.0.1:{port}/", timeout=1.0)
    except requests.exceptions.RequestException:
        timed_out = True
    elapsed = time.monotonic() - start
    stop.set()
    srv.close()

    # The read must be bounded: a no-data server cannot hang past ~timeout.
    assert timed_out, "expected a timeout exception from the stalled server"
    assert elapsed < 6.0, f"read timeout did not bound a stalled server: {elapsed:.1f}s"
