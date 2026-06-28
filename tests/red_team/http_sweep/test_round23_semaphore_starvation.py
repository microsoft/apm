"""Round-23 red-team: _HTTP_INFLIGHT permit reclamation under slow-loris.

Round-22 added ``_HTTP_INFLIGHT = BoundedSemaphore(MAX_HTTP_DISPATCH_THREADS)``
(32) to bound how many live+abandoned ``apm-http-post`` daemons can accumulate
across a flood of slow http entries. Round 23 found that a single fixed-size
semaphore is STARVABLE: the permit was released only when the inner
``requests.post`` truly RETURNED, so a round-21-style CONTINUOUS slow-loris
endpoint (one that never finishes the response) let an ABANDONED
(past-total-deadline) worker pin its permit for the WHOLE process lifetime.
As few as 32 continuous-dribble http entries (a malicious dependency's
apm.yml; there is no per-config cap on http entries) then permanently pinned
ALL 32 permits, and every SUBSEQUENT honest dispatch -- a first-party webhook
/ telemetry fired by a LATER lifecycle event or a different package in the
SAME ``apm install`` run -- was non-blocking-dropped: a global EGRESS
STARVATION primitive.

THE FIX (asserted here): a bounded semaphore alone cannot fix starvation; the
abandoned daemon must be UNWEDGED. The dispatch records the worker's connect
socket (a thread-local recording mixin on the pinned/capturing connection) and,
at the total deadline, force-closes it (``socket.shutdown(SHUT_RDWR)``). That
unblocks the wedged header-read in milliseconds, so the worker's ``finally``
RELEASES the ``_HTTP_INFLIGHT`` permit PROMPTLY -- even while the endpoint is
still dribbling. The semaphore therefore returns to full and never starves
honest egress; corporate-proxy egress (the proxy path is recorded the same way)
is preserved.

These probes drive the REAL ``script_executors`` dispatch path and assert
observed facts (semaphore value, whether the legit POST reached ``requests.post``,
the proxies kwarg) -- never URL substrings. They assert the SECURE reclaim
contract and FAILED on the pre-fix head (permit pinned past the deadline).
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time

import pytest

from apm_cli.core import script_executors as se

# Captured before the http_sweep conftest's autouse ``hermetic_dns`` fixture
# patches ``socket.getaddrinfo`` (the same global module object) to a fixed
# public IP. Restored per-test so a REAL loopback listener resolves honestly --
# otherwise urllib3 would dial 93.184.216.34:0 instead of 127.0.0.1 and the
# dribble worker would fail fast (releasing its permit) instead of pinning it.
_REAL_GETADDRINFO = socket.getaddrinfo

# The http_sweep conftest's autouse fixture neutralizes _get_capturing_session
# (returns None -> hermetic dispatch via mocked requests.post). The reclaim
# tests need the REAL builder so the capturing session records and force-closes
# the abandoned worker's socket. Capture it here (real, at import) and restore
# it in _direct_path.
_REAL_GET_CAPTURING = se._get_capturing_session


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ContinuousDribbleServer:
    """Loopback server that dribbles header bytes FOREVER (never terminates).

    Models the round-21-accepted continuous slow-loris collector: it sends a
    status line then an unending stream of single bytes WITHOUT the terminating
    CRLFCRLF, so the client stays blocked in header-read, resetting its per-recv
    read timeout on every byte. The inner ``apm-http-post`` worker is therefore
    abandoned at the total deadline and -- unless its socket is force-closed --
    its ``requests.post`` never returns, pinning the ``_HTTP_INFLIGHT`` permit.
    """

    def __init__(self, dribble_interval: float) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(64)
        self._sock.settimeout(1.0)
        self.stop = threading.Event()
        self._interval = dribble_interval
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _ContinuousDribbleServer:
        self._t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(2.0)
            with contextlib.suppress(OSError):
                conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while not self.stop.is_set():
                conn.sendall(b"X")  # never the terminating CRLFCRLF
                time.sleep(self._interval)
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()


@pytest.fixture
def _direct_path(monkeypatch):
    """Force the direct (non-guarded) capturing path -- the guarded session
    refuses loopback by design. Resets the process-cached sessions around the
    test so socket recording is exercised end-to-end."""
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


# --------------------------------------------------------------------------
# CORE MECHANIC (secure contract): an abandoned continuous-dribble worker's
# permit is RECLAIMED PROMPTLY at the total deadline -- the force-close unblocks
# the wedged read so the worker's finally releases the permit. The endpoint is
# STILL dribbling, so a pre-fix process-lifetime pin would keep the permit held.
# --------------------------------------------------------------------------
def test_abandoned_dribble_worker_reclaims_permit_at_deadline(_direct_path):
    # quiesce any residual abandoned workers from prior tests first
    full = _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=8.0)
    assert full == se.MAX_HTTP_DISPATCH_THREADS, (
        f"semaphore not at rest before test ({full}); cannot measure reclaim"
    )

    server = _ContinuousDribbleServer(dribble_interval=0.15)
    with server:
        url = f"http://127.0.0.1:{server.port}/"
        before = se._HTTP_INFLIGHT._value
        t0 = time.monotonic()
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 0.5, "post-install", url
        )
        returned_after = time.monotonic() - t0

        # Dispatch honored the 0.5s total deadline (did not hang on the dribble).
        assert returned_after < 3.0, f"dispatch hung {returned_after:.1f}s on dribble"

        # SECURE: the permit is reclaimed promptly AT THE DEADLINE, even though
        # the endpoint is STILL dribbling -- the abandoned worker's socket was
        # force-closed, so its requests.post raised and the finally released the
        # permit. A pre-fix process-lifetime pin would keep it at before-1.
        recovered = _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=5.0)
        assert recovered == se.MAX_HTTP_DISPATCH_THREADS, (
            f"permit not reclaimed at the deadline while the endpoint still "
            f"dribbled (value={recovered}, expected {se.MAX_HTTP_DISPATCH_THREADS}); "
            "the abandoned worker is pinning its _HTTP_INFLIGHT permit -- "
            "force-close the recorded socket so the wedged read unblocks"
        )
        assert before == se.MAX_HTTP_DISPATCH_THREADS


# --------------------------------------------------------------------------
# NO STARVATION BUILDUP: repeated abandoned dribble dispatches must NOT
# cumulatively pin permits. After a run of continuous-dribble endpoints the
# semaphore returns to full, so a later honest dispatch can always acquire --
# the egress-starvation primitive is gone.
# --------------------------------------------------------------------------
def test_repeated_dribbles_do_not_cumulatively_pin_permits(_direct_path):
    full = _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=8.0)
    assert full == se.MAX_HTTP_DISPATCH_THREADS, "semaphore not at rest before test"

    server = _ContinuousDribbleServer(dribble_interval=0.15)
    with server:
        url = f"http://127.0.0.1:{server.port}/"
        for i in range(5):
            se._dispatch_http_request(
                url, "{}", {"Content-Type": "application/json"}, 0.4, "post-install", url
            )
            recovered = _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=5.0)
            assert recovered == se.MAX_HTTP_DISPATCH_THREADS, (
                f"after dribble #{i + 1} the semaphore stalled at {recovered}; "
                "permits are accumulating a pin -> starvation buildup"
            )

    # The semaphore is healthy: an honest dispatch can still acquire a permit.
    acquired = se._HTTP_INFLIGHT.acquire(blocking=False)
    try:
        assert acquired, "no permit available after dribbles -> honest egress starved"
    finally:
        if acquired:
            se._HTTP_INFLIGHT.release()


# --------------------------------------------------------------------------
# PRESERVATION CONTROL: the fix must keep corporate-proxy egress. With a free
# permit and HTTPS_PROXY set, a legit external host is delivered THROUGH the env
# proxy (NO_PROXY empty). The socket-recording fix must not regress this -- it is
# orthogonal to permit accounting and to the proxy path.
# --------------------------------------------------------------------------
def test_corporate_proxy_egress_preserved(monkeypatch):
    seen_proxies: list[dict] = []

    class _FakeResp:
        status_code = 200
        ok = True

        def close(self):
            return None

    class _FakeSession:
        def post(self, url, *a, **k):
            seen_proxies.append(k.get("proxies"))
            return _FakeResp()

    # Env proxy applies (HTTPS_PROXY set, NO_PROXY empty) so the dispatch takes
    # the proxy branch, which routes through the capturing session (records the
    # proxy socket for slow-loris force-close). Patch that seam and assert the
    # corporate proxy is passed through explicitly.
    monkeypatch.setattr(se, "_get_capturing_session", lambda: _FakeSession())
    monkeypatch.setattr(se, "_append_to_script_log", lambda *a, **k: None)
    monkeypatch.setitem(os.environ, "HTTPS_PROXY", "http://corp-proxy.example:8080")
    monkeypatch.setitem(os.environ, "NO_PROXY", "")

    # ensure a permit is available
    _wait_permits(se.MAX_HTTP_DISPATCH_THREADS, timeout=8.0)

    url = "https://collector.example.com/ingest"
    se._dispatch_http_request(
        url, "{}", {"Content-Type": "application/json"}, 10, "post-install", url
    )
    deadline = time.monotonic() + 5.0
    while not seen_proxies and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seen_proxies and seen_proxies[-1] == {"https": "http://corp-proxy.example:8080"}, (
        "corporate proxy egress was not honored on the direct path"
    )
