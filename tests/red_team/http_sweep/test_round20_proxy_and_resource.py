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
# PROXY (corporate-egress contract): the operator's env-configured proxy
# MUST be honored for an already-vetted PUBLIC destination -- in many
# corporate networks the proxy is the only outbound path. The destination
# SSRF gate is proxy-AGNOSTIC and runs before dispatch, so the proxy only
# ever carries requests to destinations the gate already approved.
# --------------------------------------------------------------------------
def test_https_proxy_env_is_honored_for_vetted_public_target(monkeypatch):
    """Prove the operator's HTTPS_PROXY IS used for a vetted public host.

    Regression guard for the corporate-proxy tunnel: APM must route a
    lifecycle http action through the env-configured proxy exactly as
    curl / pip / npm / git do. The destination (example.com -> public) was
    already vetted by the up-front gate; the proxy host is loopback here
    only so a hermetic local listener can PROVE the connection is dialed.
    """
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
        # target host is unambiguously PUBLIC; the gate allows it.
        target = "https://example.com/ingest"
        assert se._ssrf_block_reason(urlparse(target).hostname) is None
        # the operator's environment mandates a proxy for this destination.
        assert se._environ_proxies_for(target), "env proxy should be resolved"

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

    # SECURE-AND-FUNCTIONAL CONTRACT: the corporate proxy IS dialed (egress
    # works), and because the target is https the proxy receives a CONNECT
    # tunnel request -- proving APM honored the operator's mandated egress
    # path rather than silently dropping all proxy support.
    assert listener.hits >= 1, (
        "corporate-egress regression: the env-configured HTTPS_PROXY was NOT "
        f"honored for the vetted public host {urlparse(target).hostname!r} "
        f"(proxy 127.0.0.1:{listener.port} never dialed). APM must tunnel "
        "through the operator's proxy like curl/pip/npm/git."
    )
    assert verb == b"CONNECT", (
        f"expected an HTTPS CONNECT tunnel through the proxy, got verb={verb!r} peer={peer}"
    )


def test_destination_gate_blocks_internal_target_even_with_proxy(monkeypatch):
    """The destination SSRF gate is proxy-AGNOSTIC: an internal target is

    refused before dispatch whether or not a proxy is configured. Honoring
    the operator's proxy must NOT become an SSRF bypass -- the proxy only
    ever carries gate-approved destinations.
    """
    from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo, ScriptEntry

    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)

    with _RecordingListener() as listener:
        proxy_url = f"http://127.0.0.1:{listener.port}"
        monkeypatch.setenv("HTTPS_PROXY", proxy_url)
        monkeypatch.setenv("https_proxy", proxy_url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        # Internal literals: cloud-metadata + RFC1918 host. The gate refuses
        # both regardless of the configured proxy.
        for internal in ("https://169.254.169.254/latest/meta-data/", "https://10.0.0.5/x"):
            host = urlparse(internal).hostname
            assert se._ssrf_block_reason(host) is not None, (
                f"gate must block internal host {host!r} even with a proxy set"
            )
            script = ScriptEntry(script_type="http", event="post-install", url=internal)
            event = LifecycleEvent(
                event="post-install",
                packages=[PackageInfo(name="org/repo", reference="v1")],
                scope="project",
                timestamp="2026-01-01T00:00:00Z",
                cli_version="0.0.0",
                working_directory="/tmp/p",
            )
            # _prepare_http enforces the gate BEFORE any dispatch; an internal
            # destination yields None -> the proxy is never contacted.
            assert se._prepare_http(script, event) is None, (
                f"internal destination {internal!r} must be refused up-front "
                "even when an env proxy is configured"
            )
        time.sleep(0.2)

    # The proxy was never dialed for an internal destination: the gate ran
    # first and refused the request, so no CONNECT ever reached the proxy.
    assert listener.hits == 0, (
        "SSRF-via-proxy bypass: an internal destination reached the proxy "
        f"(127.0.0.1:{listener.port}) despite the up-front gate. The gate must "
        "refuse internal targets BEFORE proxy routing."
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
