"""Round-30 red-team (HTTP / SSRF / network-bounds): subtle-surface proofs.

Adversarial hardening sweep round 30. Every probe drives the REAL
``apm_cli.core.script_executors`` dispatch surface -- ``_prepare_http``,
``_dispatch_http_request``, ``dispatch_http_batch``, the cached
guarded/capturing ``requests`` sessions, and the resolve-and-pin
``_ssrf_safe_connect`` -- against REAL ``127.0.0.1`` listeners (sockets bound to
ephemeral ports) with a request-count sentinel. No dispatch is reimplemented.

This round pivots to the SUBTLE remaining surfaces called out for round-30
(http held CLEAN r24-r29):

1. Redirect-not-followed (redirect-bypass): a real plaintext server returns a
   ``302 Location:`` pointing at a loopback SENTINEL. With ``allow_redirects=False``
   the sentinel must receive ZERO hits. RED-BEFORE: dropping the flag would let
   ``requests`` follow the 3xx and dial the sentinel.
2. Proxy + resolved-internal destination (proxy-unsafe / ssrf): even with
   ``HTTPS_PROXY`` set, a host that RESOLVES to an internal address is refused
   up-front by ``_prepare_http`` and the real proxy receives NO ``CONNECT``;
   companion (NON-NEGOTIABLE) a public destination STILL tunnels ``CONNECT
   host:443`` through the corporate proxy.
3. Multi-record rebind (dns-rebind): a host resolving to BOTH a public AND a
   private record is refused at prepare (ANY internal -> refuse), and the
   connect-time pin connects ONLY to the public record -- the loopback sentinel
   is never dialed.
4. Encoded-IP SSRF via the resolver fallback (ssrf): dotted-octal / abbreviated
   forms that ``_host_to_ip_literal`` does NOT canonicalise are still caught
   because ``_ssrf_block_reason`` resolves and classifies the RESOLVED address;
   driven end-to-end through ``dispatch_http_batch`` + the REAL guarded session
   against a loopback sentinel (zero hits).
5. Sequential abandoned-daemon residual (resource-exhaustion): N sequential
   slow-loris dispatches each force-closed at the deadline must leave live
   thread + fd counts BOUNDED (not growing with N) -- the permit is reclaimed and
   the terminated worker holds no live OS thread/fd.
6. Scoped-IPv6 / IDNA-homograph hosts (ssrf): ``fe80::1%eth0`` and a homograph
   host modelled to resolve internal are refused.
7. Response body is never read (resource-exhaustion): a huge-Content-Length /
   infinite-body response only has its status line consumed; the body read
   helpers are never invoked and the response is closed.

Every URL/host assertion parses with ``urllib.parse`` (never substring) per the
repo test-convention rule.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
from typing import ClassVar
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo, ScriptEntry

# Captured at import, BEFORE the http_sweep conftest autouse fixtures patch the
# module globals to None, so probes needing the production path get the real
# session builders / resolver (mirrors round-23 / round-26 / round-29).
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


def _fd_count() -> int:
    """Best-effort open-fd count (psutil-free, macOS/Linux)."""
    for path in ("/dev/fd", "/proc/self/fd"):
        with contextlib.suppress(OSError):
            return len(os.listdir(path))
    return -1


def _capture_logs(monkeypatch) -> list[dict[str, str]]:
    logs: list[dict[str, str]] = []

    def _fake(event, stype, url, stdout="", stderr="", status=""):
        logs.append({"status": status, "stdout": stdout, "stderr": stderr, "url": url})

    monkeypatch.setattr(se, "_append_to_script_log", _fake)
    return logs


class _SentinelServer:
    """A real TCP listener on 127.0.0.1 that COUNTS accepted connections.

    Used as the 'internal' SSRF target: a secure executor must connect ZERO
    times. Optionally serves a fixed first-response then closes.
    """

    def __init__(
        self, *, response: bytes | None = None, stall: bool = False, hold: float = 30.0
    ) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self._response = response
        self._stall = stall
        self._hold = hold
        self.hits = 0
        self.first_line: str | None = None
        self.connected = threading.Event()
        self.stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _SentinelServer:
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
            self.hits += 1
            self.connected.set()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(3.0)
            with contextlib.suppress(OSError):
                data = conn.recv(8192)
                if data and self.first_line is None:
                    self.first_line = data.split(b"\r\n", 1)[0].decode("latin1")
            if self._stall:
                # Accept then go silent: a dribbling endpoint the dispatcher must
                # force-close at the deadline. Hold the conn only a BOUNDED time so
                # server-side threads/fds clear during the test's settle window
                # (otherwise they would confound an executor-side resource count).
                self.stop.wait(self._hold)
                return
            if self._response is not None:
                with contextlib.suppress(OSError):
                    conn.sendall(self._response)
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


# ---------------------------------------------------------------------------
# 1. Redirect to a loopback target is NOT followed (allow_redirects=False).
# ---------------------------------------------------------------------------
def test_redirect_to_loopback_is_not_followed(_proxy_env, monkeypatch):
    """A 302 Location pointing at a loopback sentinel must never be dialed.

    Drives the REAL capturing session over plaintext http (direct path, no env
    proxy) against a real redirector. RED-BEFORE: without ``allow_redirects=False``
    requests would follow the 302 and the sentinel would be hit.
    """
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("NO_PROXY", None)
    # Direct path through the REAL capturing session (guarded session is https
    # only; force it off so the http redirector uses the capturing session).
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    logs = _capture_logs(monkeypatch)

    with _SentinelServer() as sentinel:
        location = f"http://127.0.0.1:{sentinel.port}/internal"
        redirect = (
            b"HTTP/1.1 302 Found\r\n"
            b"Location: " + location.encode("latin1") + b"\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )
        with _SentinelServer(response=redirect) as redirector:
            url = f"http://127.0.0.1:{redirector.port}/start"
            se._dispatch_http_request(
                url, "{}", {"Content-Type": "application/json"}, 4.0, "post-install", url
            )

            # The redirector itself was contacted (1 hit) ...
            assert redirector.connected.wait(3.0)
            # ... but the loopback sentinel in the Location was NEVER dialed.
            assert not sentinel.connected.wait(0.8)
            assert sentinel.hits == 0
    # The 302 was logged as a terminal response, not followed.
    assert logs, "dispatch must log an outcome"


# ---------------------------------------------------------------------------
# 2. Proxy set + destination RESOLVES internal -> refused up-front (no CONNECT);
#    companion: public destination STILL tunnels (corporate egress preserved).
# ---------------------------------------------------------------------------
def test_proxy_set_resolved_internal_refused_and_public_tunnels(_proxy_env, monkeypatch):
    with _SentinelServer(stall=True) as proxy:
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.port}"
        os.environ.pop("NO_PROXY", None)
        monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)

        # Resolver: an attacker hostname that RESOLVES (not a literal) to an
        # internal address; the public telemetry host resolves public. Anything
        # else (notably the 127.0.0.1 PROXY host) delegates to the real resolver
        # so the proxy is honestly dialed in part (b).
        def _resolver(host, *a, **k):
            if host == "telemetry.evil.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
            if host == "updates.example.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
            return _REAL_GETADDRINFO(host, *a, **k)

        monkeypatch.setattr(socket, "getaddrinfo", _resolver)

        # (a) Resolved-internal destination is refused BEFORE any egress, even
        # though a proxy is configured.
        bad = ScriptEntry(
            script_type="http", event="post-install", url="https://telemetry.evil.test/x"
        )
        assert se._prepare_http(bad, _make_event()) is None
        assert not proxy.connected.wait(0.5)
        assert proxy.first_line is None

        # (b) NON-NEGOTIABLE: a public destination still tunnels CONNECT host:443
        # through the corporate proxy (curl/pip/npm parity).
        public_url = "https://updates.example.test/telemetry"
        se._dispatch_http_request(
            public_url, "{}", {"Content-Type": "application/json"}, 3.0, "post-install", public_url
        )
        assert proxy.connected.wait(4.0)
        assert proxy.first_line is not None
        method, target, _proto = proxy.first_line.split(" ", 2)
        assert method == "CONNECT"
        host, _, port = target.partition(":")
        assert host == urlsplit(public_url).hostname
        assert port == "443"


# ---------------------------------------------------------------------------
# 3. Multi-record rebind: host resolves to BOTH public + private.
# ---------------------------------------------------------------------------
def test_multi_record_public_and_private_refused_and_pin_skips_internal(_proxy_env, monkeypatch):
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("NO_PROXY", None)

    with _SentinelServer() as sentinel:
        # A name that answers with a PUBLIC record AND the loopback sentinel.
        def _resolver(host, *a, **k):
            if host == "dualstack.evil.test":
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", sentinel.port)),
                ]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", _resolver)

        # (a) Up-front gate refuses: ANY internal record poisons the destination.
        reason = se._ssrf_block_reason("dualstack.evil.test")
        assert reason is not None
        bad = ScriptEntry(
            script_type="http", event="post-install", url="https://dualstack.evil.test/x"
        )
        assert se._prepare_http(bad, _make_event()) is None

        # (b) The connect-time pin, given the SAME multi-record answer, must
        # connect ONLY to the public record and NEVER dial the loopback sentinel.
        with contextlib.suppress(OSError, se._SSRFConnectError):
            s = se._ssrf_safe_connect(("dualstack.evil.test", sentinel.port), timeout=1.0)
            with contextlib.suppress(OSError):
                s.close()
        assert not sentinel.connected.wait(0.6)
        assert sentinel.hits == 0


# ---------------------------------------------------------------------------
# 4. Encoded-IP SSRF caught by the resolver fallback, end-to-end (zero hits).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("encoded", ["0177.1", "0x7f.1", "017700000001"])
def test_encoded_ip_resolves_internal_refused_end_to_end(_proxy_env, monkeypatch, encoded):
    """Dotted-octal/hex forms ``_host_to_ip_literal`` does not canonicalise.

    The gate resolves them and classifies the RESOLVED address, so a real
    loopback sentinel is never dialed. Driven through the REAL ``dispatch_http_batch``
    seam + REAL guarded session.
    """
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("NO_PROXY", None)
    monkeypatch.setattr(se, "_get_guarded_session", _REAL_GET_GUARDED)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None
    logs = _capture_logs(monkeypatch)

    with _SentinelServer() as sentinel:
        # Model the C-resolver semantics deterministically: these encodings
        # denote 127.0.0.1 (verified on the host resolver). The gate must catch
        # them via the RESOLVED address regardless of literal canonicalisation.
        def _resolver(host, *a, **k):
            if host.rstrip(".") == encoded:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", sentinel.port))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", _resolver)

        # The gate must refuse this destination outright.
        assert se._ssrf_block_reason(encoded) is not None

        url = f"https://{encoded}:{sentinel.port}/x"
        script = ScriptEntry(script_type="http", event="post-install", url=url)
        workers = se.dispatch_http_batch([script], _make_event())
        for w in workers:
            w.join(timeout=5)

        # Loopback sentinel never dialed; no dispatch egress at all.
        assert not sentinel.connected.wait(0.6)
        assert sentinel.hits == 0
        # And no successful HTTP outcome was logged (refused before dispatch).
        assert all(log.get("status") != "ok" for log in logs)


# ---------------------------------------------------------------------------
# 5. Sequential abandoned slow-loris daemons: live thread + fd counts bounded.
# ---------------------------------------------------------------------------
def test_sequential_force_closed_dispatches_bounded(_proxy_env, monkeypatch):
    """N sequential slow-loris dispatches must NOT accumulate threads/fds.

    Each dispatch connects to a real loopback stall server (via the REAL
    capturing session, direct path) and is force-closed at a short deadline. A
    secure executor reclaims the permit and the terminated worker holds no live
    OS thread/fd, so counts stay bounded regardless of N. The stall server holds
    each connection only briefly (bounded ``hold``) so its own per-conn
    threads/sockets clear during the settle window and do not confound the
    executor-side count. RED-BEFORE: an executor leak would grow the live
    ``apm-http-post`` worker count / fd count monotonically with N.
    """
    import os as _os

    def _http_post_threads() -> int:
        return sum(1 for t in threading.enumerate() if t.name == "apm-http-post" and t.is_alive())

    watchdog = threading.Timer(90.0, lambda: _os._exit(99))
    watchdog.daemon = True
    watchdog.start()
    try:
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("NO_PROXY", None)
        # http stall server via REAL capturing session, direct path.
        monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
        monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
        monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
        _capture_logs(monkeypatch)

        hold = 2.5
        with _SentinelServer(stall=True, hold=hold) as stall:
            url = f"http://127.0.0.1:{stall.port}/slow"

            def _one():
                se._dispatch_http_request(
                    url, "{}", {"Content-Type": "application/json"}, 1.0, "post-install", url
                )

            # Warm one dispatch so lazy imports / pools are established before
            # we sample the baseline.
            _one()
            time.sleep(hold + 1.0)
            threads_base = threading.active_count()
            fds_base = _fd_count()
            permits_base = se._HTTP_INFLIGHT._value

            n = 24
            for _ in range(n):
                _one()

            # Settle past the server's bounded hold so BOTH the executor workers
            # and the server's per-conn threads/sockets have torn down.
            settle = time.monotonic() + (hold + 12.0)
            while time.monotonic() < settle:
                if se._HTTP_INFLIGHT._value >= permits_base and _http_post_threads() == 0:
                    break
                time.sleep(0.1)

            threads_after = threading.active_count()
            fds_after = _fd_count()

            # Permit fully reclaimed (no semaphore leak under serial force-close).
            assert se._HTTP_INFLIGHT._value == permits_base
            # No executor worker threads remain alive after the flood.
            assert _http_post_threads() == 0, "executor worker threads leaked"
            # Live threads bounded: must NOT grow by ~N (=24).
            assert threads_after <= threads_base + 6, (
                f"thread leak: base={threads_base} after={threads_after} N={n}"
            )
            # fd count bounded similarly (only meaningful where enumerable).
            if fds_base >= 0 and fds_after >= 0:
                assert fds_after <= fds_base + 6, (
                    f"fd leak: base={fds_base} after={fds_after} N={n}"
                )
    finally:
        watchdog.cancel()


# ---------------------------------------------------------------------------
# 6. Scoped-IPv6 zone-id and IDNA-homograph hosts are refused.
# ---------------------------------------------------------------------------
def test_scoped_ipv6_and_homograph_refused(_proxy_env, monkeypatch):
    # (a) Scoped link-local literal: classified internal by the literal path.
    assert se._ssrf_block_reason("fe80::1%eth0") is not None
    bracket = ScriptEntry(
        script_type="http", event="post-install", url="https://[fe80::1%25eth0]/x"
    )
    # urlparse exposes the zone-id host; the gate refuses it (or fail-closed None).
    assert se._prepare_http(bracket, _make_event()) is None

    # (b) IDNA-homograph host modelled to resolve to an internal address.
    def _resolver(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolver)
    homo = ScriptEntry(
        script_type="http", event="post-install", url="https://m\u0435tadata.internal.test/x"
    )
    assert se._prepare_http(homo, _make_event()) is None


# ---------------------------------------------------------------------------
# 7. Response body is never read (huge / infinite body cannot exhaust memory).
# ---------------------------------------------------------------------------
def test_response_body_never_read(_proxy_env, monkeypatch):
    """Only the status line is consumed; body read helpers are never called.

    RED-BEFORE: a code path that read ``resp.content`` / iterated the body would
    trip the exploding accessors and fail.
    """
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("NO_PROXY", None)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    logs = _capture_logs(monkeypatch)

    closed = threading.Event()

    class _ExplodingBody:
        status_code = 200
        ok = True
        # Advertise a 100 GiB body; reading any of it must never happen.
        headers: ClassVar = {"Content-Length": str(100 * 1024**3)}

        def close(self):
            closed.set()

        def __getattr__(self, name):
            if name in ("content", "text", "iter_content", "iter_lines", "raw", "json"):
                raise AssertionError(f"executor read response body via .{name}")
            raise AttributeError(name)

    def _fake_post(url, *a, **k):
        assert k.get("stream") is True, "stream=True must bound the body read"
        return _ExplodingBody()

    with patch("requests.post", side_effect=_fake_post):
        # Force the bare-requests fallback so our fake post is used.
        monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
        monkeypatch.setattr(se, "_get_capturing_session", lambda: None)
        url = "https://updates.example.test/telemetry"
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 3.0, "post-install", url
        )

    assert closed.is_set(), "response must be closed without reading the body"
    assert logs and logs[-1]["status"] == "ok"
