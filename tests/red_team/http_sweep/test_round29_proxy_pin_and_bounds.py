"""Round-29 red-team (HTTP / SSRF / network-bounds): real-surface proofs.

Adversarial hardening sweep round 29. Every probe drives the REAL
``apm_cli.core.script_executors`` dispatch surface -- ``_prepare_http``,
``_dispatch_http_request``, the cached guarded/capturing ``requests`` sessions,
and the resolve-and-pin ``_ssrf_safe_connect`` -- against REAL ``127.0.0.1``
listeners (a socket bound to an ephemeral port) and a REAL local proxy process.
No dispatch is reimplemented.

Angles covered this round (all DEFENDED on head 427ed91d; these assert the
SECURE property and PASS):

1. Destination SSRF gate runs UP-FRONT and proxy-agnostic: an internal literal
   destination is refused by ``_prepare_http`` even with ``HTTPS_PROXY`` set, and
   the real proxy listener receives NO ``CONNECT`` (no egress at all).
2. Corporate-proxy egress is PRESERVED (non-negotiable): a public destination
   with ``HTTPS_PROXY`` set emits ``CONNECT <public-host>:443`` to the real proxy
   (curl/pip/npm parity) via the capturing session.
3. The connect-time DNS pin is defense-in-depth: calling ``_dispatch_http_request``
   directly (bypassing the prepare gate) at ``https://127.0.0.1:<port>/`` is
   refused inside the REAL guarded session by ``_ssrf_safe_connect`` -- the
   loopback listener is never dialed.
4. NAT64 / 6to4 / Teredo / CGNAT-embedded internal literals are refused by the
   destination gate (the stdlib ``ipaddress`` predicates flag them; the gate does
   not mis-classify the embedded-internal forms as public).
5. A slow-loris-at-the-proxy that stalls after ``CONNECT`` is bounded by the
   total deadline and its in-flight permit is fully reclaimed (no leak), proven
   with a daemon-thread watchdog that ``os._exit(99)``s on an unbounded hang.

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

# Captured at import, BEFORE the http_sweep conftest autouse fixture patches the
# module globals to None, so probes that need the production path get the real
# session builders (mirrors round-23 / round-26).
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_GET_GUARDED = se._get_guarded_session
_REAL_GET_CAPTURING = se._get_capturing_session


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_event() -> LifecycleEvent:
    return LifecycleEvent(
        event="post-install",
        packages=[PackageInfo(name="org/repo", reference="v1")],
        scope="project",
        timestamp="2026-01-01T00:00:00Z",
        cli_version="0.0.0",
        working_directory="/home/victim/project",
    )


class _RecordingProxy:
    """A real TCP listener on 127.0.0.1 that records the first request line.

    Models a corporate proxy just enough to capture the ``CONNECT`` line (or to
    prove no bytes ever arrive). It never completes the tunnel, so the client
    fails fast after the line is captured.
    """

    def __init__(self, *, stall: bool = False) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self._stall = stall
        self.first_line: str | None = None
        self.connected = threading.Event()
        self.stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _RecordingProxy:
        self._t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        self.connected.set()
        try:
            conn.settimeout(3.0)
            with contextlib.suppress(OSError):
                data = conn.recv(4096)
                if data:
                    self.first_line = data.split(b"\r\n", 1)[0].decode("latin1")
            if self._stall:
                # Accept the CONNECT then go silent: a dribbling proxy. The
                # dispatcher must force-close at the total deadline.
                while not self.stop.wait(0.2):
                    pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()


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


def _capture_logs(monkeypatch) -> list[dict[str, str]]:
    logs: list[dict[str, str]] = []

    def _fake(event, stype, url, stdout="", stderr="", status=""):
        logs.append({"status": status, "stdout": stdout, "stderr": stderr, "url": url})

    monkeypatch.setattr(se, "_append_to_script_log", _fake)
    return logs


# ---------------------------------------------------------------------------
# 1. Destination gate is proxy-agnostic: a blocked literal destination is
#    refused up-front; the proxy never sees a CONNECT.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "blocked_url",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/",
        "https://10.0.0.5/",
        "https://127.0.0.1/",
    ],
)
def test_destination_gate_refuses_internal_even_with_proxy(_proxy_env, monkeypatch, blocked_url):
    with _RecordingProxy() as proxy:
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.port}"
        os.environ.pop("NO_PROXY", None)
        monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)

        script = ScriptEntry(script_type="http", event="post-install", url=blocked_url)
        prepared = se._prepare_http(script, _make_event())

        # Refused up-front, regardless of the configured proxy.
        assert prepared is None
        # And nothing was dialed: the proxy received no CONNECT.
        assert not proxy.connected.wait(0.5)
        assert proxy.first_line is None


# ---------------------------------------------------------------------------
# 2. Corporate-proxy egress preserved (NON-NEGOTIABLE): a public destination
#    with HTTPS_PROXY set emits CONNECT <public-host>:443 to the proxy.
# ---------------------------------------------------------------------------
def test_corporate_proxy_egress_emits_connect(_proxy_env, monkeypatch):
    with _RecordingProxy() as proxy:
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.port}"
        os.environ.pop("NO_PROXY", None)
        # Real capturing session + real getaddrinfo so the proxy is honestly
        # dialed (the guarded session does not apply on the proxy path).
        monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
        monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)

        # ``example.test`` does not resolve locally (RFC 6761), so the up-front
        # gate fail-opens (None) -- the corporate proxy owns this hop's resolution.
        url = "https://example.test/telemetry"
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 5.0, "post-install", url
        )

        assert proxy.connected.wait(4.0)
        assert proxy.first_line is not None
        # CONNECT parity with curl/pip/npm: tunnel target is the PUBLIC host:443.
        method, target, _proto = proxy.first_line.split(" ", 2)
        assert method == "CONNECT"
        host, _, port = target.partition(":")
        assert host == urlsplit(url).hostname
        assert port == "443"


# ---------------------------------------------------------------------------
# 3. Connect-time DNS pin is defense-in-depth: dispatching DIRECTLY to a
#    loopback listener through the REAL guarded session is refused at connect.
# ---------------------------------------------------------------------------
def test_connect_pin_refuses_loopback_on_guarded_session(_proxy_env, monkeypatch):
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    monkeypatch.setattr(se, "_get_guarded_session", _REAL_GET_GUARDED)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    logs = _capture_logs(monkeypatch)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(1.5)
    port = srv.getsockname()[1]
    hit = threading.Event()

    def _serve():
        try:
            conn, _ = srv.accept()
            hit.set()
            conn.close()
        except OSError:
            pass

    threading.Thread(target=_serve, daemon=True).start()

    url = f"https://127.0.0.1:{port}/"
    # Bypass _prepare_http on purpose to isolate the SECOND layer (the pin).
    se._dispatch_http_request(
        url, "{}", {"Content-Type": "application/json"}, 3.0, "post-install", url
    )

    assert se._get_guarded_session() is not None, "guarded session must build"
    # The pin refused the dial: the loopback listener was never connected.
    assert not hit.wait(0.5)
    assert logs and logs[-1]["status"] == "error"
    with contextlib.suppress(OSError):
        srv.close()


# ---------------------------------------------------------------------------
# 4. NAT64 / 6to4 / Teredo / CGNAT-embedded internal literals are refused.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host",
    [
        "64:ff9b::a9fe:a9fe",  # NAT64 well-known prefix of 169.254.169.254
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "2002:a9fe:a9fe::",  # 6to4 of 169.254.169.254
        "2002:7f00:1::",  # 6to4 of 127.0.0.1
        "2001:0:0:0:0:0:a9fe:a9fe",  # Teredo embedding 169.254.169.254
        "100.64.0.1",  # CGNAT shared space
        "::ffff:169.254.169.254",  # IPv4-mapped link-local
    ],
)
def test_nat64_6to4_teredo_internal_literals_refused(host):
    reason = se._ssrf_block_reason(host)
    assert reason is not None, f"{host} must be refused by the destination gate"

    # And the full prepare path refuses it too (HTTPS literal authority).
    bracket = f"[{host}]" if ":" in host else host
    script = ScriptEntry(script_type="http", event="post-install", url=f"https://{bracket}/")
    assert se._prepare_http(script, _make_event()) is None


# ---------------------------------------------------------------------------
# 5. A proxy that stalls after CONNECT is bounded by the total deadline and the
#    in-flight permit is fully reclaimed. Daemon watchdog kills an unbounded hang.
# ---------------------------------------------------------------------------
def test_stalled_proxy_bounded_and_permit_reclaimed(_proxy_env, monkeypatch):
    import os as _os

    # Hard watchdog: if the dispatch hangs unbounded, fail the whole process so
    # the break is unmistakable (never fires on a defended head).
    watchdog = threading.Timer(20.0, lambda: _os._exit(99))
    watchdog.daemon = True
    watchdog.start()
    try:
        with _RecordingProxy(stall=True) as proxy:
            os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.port}"
            os.environ.pop("NO_PROXY", None)
            monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
            monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
            logs = _capture_logs(monkeypatch)

            permits_before = se._HTTP_INFLIGHT._value

            url = "https://example.test/telemetry"
            deadline = 2.0
            start = time.monotonic()
            se._dispatch_http_request(
                url,
                "{}",
                {"Content-Type": "application/json"},
                deadline,
                "post-install",
                url,
            )
            elapsed = time.monotonic() - start

            # Bounded by total deadline + abandon grace (+ scheduling slack).
            assert elapsed < deadline + se._HTTP_ABANDON_GRACE + 5.0
            assert logs and logs[-1]["status"] == "error"

            # Proxy did receive the CONNECT (it stalled afterwards).
            assert proxy.connected.wait(1.0)

            # Permit fully reclaimed: no leak under a stalled proxy.
            reclaim_deadline = time.monotonic() + 5.0
            while time.monotonic() < reclaim_deadline:
                if se._HTTP_INFLIGHT._value >= permits_before:
                    break
                time.sleep(0.05)
            assert se._HTTP_INFLIGHT._value == permits_before
    finally:
        watchdog.cancel()
