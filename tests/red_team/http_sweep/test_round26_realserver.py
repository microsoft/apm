"""Round-26 red-team (HTTP): real-loopback-server proofs for the response
decompression-bomb, connection-reuse permit-leak, permit-flood accounting, and
redirect-not-followed vectors.

Mirrors the round-23 harness: forces the REAL capturing session + REAL
getaddrinfo so a 127.0.0.1 listener is dialed honestly, resets the
process-cached sessions around each test, and uses a daemon-thread watchdog
(the dispatcher's own total deadline) to bound any hang. Every probe drives the
REAL ``script_executors`` dispatch path and asserts the SECURE contract; all
PASS on the current head, documenting the vectors as defended.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
import zlib

import pytest

from apm_cli.core import script_executors as se

# Captured before the http_sweep conftest autouse fixtures patch the shared
# module globals, exactly as round-23 does, so the real loopback path is used.
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_GET_CAPTURING = se._get_capturing_session


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def _direct_path(monkeypatch):
    """Force the direct capturing path against a real loopback listener."""
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None
    yield
    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None


def _wait_permits(target: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if se._HTTP_INFLIGHT._value >= target:
            return se._HTTP_INFLIGHT._value
        time.sleep(0.05)
    return se._HTTP_INFLIGHT._value


# ---------------------------------------------------------------------------
# Decompression bomb over the wire: server lies about Content-Length AND sets
# Content-Encoding: gzip with a real bomb seed. A dispatcher that READ the body
# would hang on the Content-Length lie (or inflate the bomb); reading only the
# status line returns immediately. Deterministic, never actually inflates.
# ---------------------------------------------------------------------------
class _GzipBombServer:
    def __init__(self) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(16)
        self._sock.settimeout(1.0)
        self.stop = threading.Event()
        self.body_fully_pulled = threading.Event()
        # ~64 MiB of zeros -> tiny gzip; decompresses huge. We only ever SEND the
        # compressed bytes; the client must not read them.
        self._bomb = zlib.compress(b"\x00" * (64 * 1024 * 1024), 9)
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(3.0)
            with contextlib.suppress(OSError):
                conn.recv(4096)
            # Content-Length lies (claims far more than we send): a real body
            # read would block waiting for bytes that never come.
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 1073741824\r\n\r\n"
            )
            conn.sendall(self._bomb)
            self.body_fully_pulled.set()  # we finished pushing what we will send
        except OSError:
            return
        finally:
            # keep the conn briefly so a buggy client reading the body would hang
            time.sleep(0.5)
            with contextlib.suppress(OSError):
                conn.close()


def test_response_gzip_bomb_over_wire_not_read(_direct_path):
    """Dispatch returns promptly and never reads/inflates a gzip-bomb body."""
    full = _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=8.0)
    assert full == se.MAX_HTTP_DISPATCH_THREADS, "semaphore not at rest before test"

    with _GzipBombServer() as srv:
        url = f"http://127.0.0.1:{srv.port}/"
        t0 = time.monotonic()
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 5.0, "post-install", url
        )
        elapsed = time.monotonic() - t0

        # If the body had been read, the Content-Length lie would have wedged the
        # read until the (5s) deadline force-close. Reading only the status line
        # returns well under that. The 5s deadline gives ample slack on CI.
        assert elapsed < 3.0, (
            f"dispatch took {elapsed:.1f}s -> it began reading the bomb body "
            "(Content-Length lie wedged the read); body must never be consumed"
        )
        # Permit reclaimed (worker finished cleanly after the status line).
        recovered = _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=5.0)
        assert recovered == se.MAX_HTTP_DISPATCH_THREADS, (
            f"permit not reclaimed after gzip-bomb dispatch (value={recovered})"
        )


# ---------------------------------------------------------------------------
# Connection-reuse permit-leak hypothesis (hint #3): a pooled keep-alive conn
# reused WITHOUT calling connect() skips the socket-recording mixin, so a
# slow-loris on the reused conn could not be force-closed -> permit leak.
# PROOF that the gap is unreachable: stream=True + resp.close() WITHOUT reading
# the body discards the connection (never returned to the pool), so a second
# dispatch always opens a NEW connection and records its socket.
# ---------------------------------------------------------------------------
class _CountingKeepAliveServer:
    """Every NEW connection gets a clean 200 CL:0 keep-alive response. Counts
    how many distinct TCP connections were accepted."""

    def __init__(self) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(16)
        self._sock.settimeout(1.0)
        self.stop = threading.Event()
        self.conns_accepted = 0
        self._lock = threading.Lock()
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            with self._lock:
                self.conns_accepted += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(3.0)
            while not self.stop.is_set():
                data = b""
                try:
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        data += chunk
                except OSError:
                    return
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def test_streamed_dispatch_does_not_pool_connection(_direct_path):
    """Two sequential dispatches open two distinct connections (no pool reuse),
    so every dispatch's socket is recorded -> force-close always possible."""
    _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=8.0)
    with _CountingKeepAliveServer() as srv:
        url = f"http://127.0.0.1:{srv.port}/"
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 5.0, "post-install", url
        )
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 5.0, "post-install", url
        )
        time.sleep(0.4)
        assert srv.conns_accepted >= 2, (
            f"only {srv.conns_accepted} TCP connection(s) for 2 dispatches -> a "
            "connection was reused from the pool; a reused conn skips connect() "
            "and the socket-recording mixin, so a slow-loris on it could not be "
            "force-closed (permit-leak gap). stream+close-without-read must "
            "discard the connection."
        )
        recovered = _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=5.0)
        assert recovered == se.MAX_HTTP_DISPATCH_THREADS


# ---------------------------------------------------------------------------
# Permit-flood accounting (hint #2): more concurrent slow dispatches than the
# cap must DROP the overflow (non-blocking acquire) and reclaim every permit at
# the deadline -- never a permanent leak that silently drops later honest
# notifications.
# ---------------------------------------------------------------------------
class _ContinuousDribbleServer:
    def __init__(self, interval: float) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(128)
        self._sock.settimeout(1.0)
        self.stop = threading.Event()
        self._interval = interval
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(2.0)
            with contextlib.suppress(OSError):
                conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while not self.stop.is_set():
                conn.sendall(b"X")
                time.sleep(self._interval)
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def test_overcap_flood_drops_not_leaks_and_reclaims(_direct_path):
    """Fire more concurrent continuous-dribble dispatches than the cap.

    The non-blocking acquire means the overflow is DROPPED (logged), never
    queued or leaked; after the deadline force-close, ALL permits reclaim to
    full so a later honest dispatch can always acquire. No permanent starvation.
    """
    full = _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=10.0)
    assert full == se.MAX_HTTP_DISPATCH_THREADS, "semaphore not at rest before test"

    drops: list[str] = []
    _orig_log = se._append_to_script_log

    def _spy_log(event, kind, url, *, stdout="", stderr="", status="ok", **k):
        if "too many in-flight" in (stderr or ""):
            drops.append(stderr)

    se._append_to_script_log = _spy_log
    try:
        with _ContinuousDribbleServer(interval=0.15) as srv:
            url = f"http://127.0.0.1:{srv.port}/"
            n = se.MAX_HTTP_DISPATCH_THREADS + 8

            def _fire():
                se._dispatch_http_request(
                    url, "{}", {"Content-Type": "application/json"}, 1.0, "post-install", url
                )

            threads = [threading.Thread(target=_fire, daemon=True) for _ in range(n)]
            for t in threads:
                t.start()
            # Bounded watchdog: each dispatch honors its own 1.0s deadline; give
            # generous slack then assert no thread is wedged.
            for t in threads:
                t.join(timeout=10.0)
            assert all(not t.is_alive() for t in threads), (
                "a dispatch thread is wedged past its deadline -> the total "
                "deadline / force-close did not bound the flood"
            )
            # Overflow beyond the cap was dropped (non-blocking acquire), not
            # silently queued/leaked.
            assert drops, (
                "no overflow drop observed under a >cap flood -> acquire is not "
                "non-blocking (would queue/leak instead of dropping)"
            )
    finally:
        se._append_to_script_log = _orig_log

    # The crux: after the flood + deadline, every permit is reclaimed -- honest
    # egress is never permanently starved.
    recovered = _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=10.0)
    assert recovered == se.MAX_HTTP_DISPATCH_THREADS, (
        f"permits not fully reclaimed after flood (value={recovered}); a leak "
        "would permanently drop later honest lifecycle notifications"
    )


# ---------------------------------------------------------------------------
# Redirect-not-followed (hint #5): a 30x to an internal Location must NOT be
# followed -- allow_redirects=False means zero hops, so redirect-SSRF is fully
# out of model. The dispatcher reads the 30x status and closes; it never opens a
# connection to the redirect target.
# ---------------------------------------------------------------------------
class _RedirectToInternalServer:
    def __init__(self) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(16)
        self._sock.settimeout(1.0)
        self.stop = threading.Event()
        self.requests_seen = 0
        self._lock = threading.Lock()
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(3.0)
            with contextlib.suppress(OSError):
                conn.recv(4096)
            with self._lock:
                self.requests_seen += 1
            conn.sendall(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: http://169.254.169.254/latest/meta-data/\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def test_redirect_to_internal_is_not_followed(_direct_path):
    """A 302 -> metadata Location is logged but never followed (zero hops)."""
    _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=8.0)
    with _RedirectToInternalServer() as srv:
        url = f"http://127.0.0.1:{srv.port}/"
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 5.0, "post-install", url
        )
        time.sleep(0.4)
        assert srv.requests_seen == 1, (
            f"server saw {srv.requests_seen} requests -> a redirect hop was "
            "followed; allow_redirects=False must mean zero hops"
        )
        recovered = _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=5.0)
        assert recovered == se.MAX_HTTP_DISPATCH_THREADS
