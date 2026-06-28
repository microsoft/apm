"""Round-32 red-team (HTTP / resource-bounds): permit accounting under CHAOS.

http held CLEAN r24-r31. Round-32 PIVOT #1: drive the REAL
``_dispatch_http_request`` with 40+ CONCURRENT dispatches mixing fast / RST /
continuous-dribble(abandoned) endpoints and assert the ``_HTTP_INFLIGHT``
``BoundedSemaphore`` (MAX=32):

  * never LEAKS a permit (a never-released permit on some exception path before
    the worker's ``finally``) -- after every dispatch settles the value returns
    to MAX exactly, and
  * never DOUBLE-RELEASES (a ``BoundedSemaphore`` raises ``ValueError`` on an
    over-release) -- a tracking wrapper captures any such ValueError raised
    inside an abandoned daemon's ``finally`` (where it would otherwise be lost
    to the daemon's stack).

These probes drive the REAL dispatch helper against REAL ``127.0.0.1``
listeners (no guard/dispatch reimplementation) and assert observed facts (the
semaphore value, captured release errors) -- never URL substrings. The secure
contract: regardless of attacker-chosen concurrency / failure mix, live +
abandoned dispatches are bounded and the permit pool is conserved.

Corporate HTTPS_PROXY egress is unaffected by these probes (no proxy env set;
the direct capturing path is exercised); the egress non-regression is asserted
in ``test_round32_deadline_headers_egress.py``.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading
import time

import pytest

from apm_cli.core import script_executors as se

# Real resolver captured BEFORE the http_sweep conftest autouse fixture patches
# socket.getaddrinfo to a fixed public IP -- a real loopback listener must
# resolve honestly so urllib3 dials 127.0.0.1, not 93.184.216.34:0.
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_GET_CAPTURING = se._get_capturing_session


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ChaosServer:
    """One loopback listener whose per-connection behavior is chosen per accept.

    mode == "fast": read request, send a complete 200, close (worker returns
        promptly, releases its permit normally).
    mode == "rst": abort the connection mid-handshake (worker's post raises a
        ConnectionError -> except path -> finally releases).
    mode == "dribble": send a status line then dribble single bytes forever
        WITHOUT terminating CRLFCRLF (worker wedged in header-read -> abandoned
        at the total deadline -> force-closed -> finally releases).
    """

    def __init__(self, mode: str, dribble_interval: float = 0.1) -> None:
        self.mode = mode
        self._interval = dribble_interval
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(128)
        self._sock.settimeout(0.5)
        self.stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _ChaosServer:
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
        try:
            conn.settimeout(2.0)
            if self.mode == "rst":
                with contextlib.suppress(OSError):
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return
            with contextlib.suppress(OSError):
                conn.recv(4096)
            if self.mode == "fast":
                with contextlib.suppress(OSError):
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                return
            # dribble
            with contextlib.suppress(OSError):
                conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while not self.stop.is_set():
                try:
                    conn.sendall(b"X")  # never the terminating CRLFCRLF
                except OSError:
                    return
                self.stop.wait(self._interval)
        finally:
            with contextlib.suppress(OSError):
                conn.close()


class _TrackingSemaphore(threading.BoundedSemaphore):
    """BoundedSemaphore that captures any over-release ValueError.

    A double-release inside an abandoned daemon's ``finally`` would raise
    ``ValueError`` on the daemon stack and be lost. By recording it here we can
    assert it never happened, and we also track the lowest value seen.
    """

    def __init__(self, value: int) -> None:
        super().__init__(value)
        self.release_errors: list[str] = []
        self.min_seen = value
        self._track_lock = threading.Lock()

    def acquire(self, *a, **k):  # type: ignore[override]
        got = super().acquire(*a, **k)
        if got:
            with self._track_lock:
                self.min_seen = min(self.min_seen, self._value)
        return got

    def release(self, *a, **k):  # type: ignore[override]
        try:
            return super().release(*a, **k)
        except ValueError as exc:  # over-release -> double-release bug
            with self._track_lock:
                self.release_errors.append(str(exc))
            raise


@pytest.fixture
def _chaos_env(monkeypatch):
    """Production direct (capturing) path + honest loopback DNS + tracking sem."""
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)

    logs: list[dict] = []

    def _fake_log(event, stype, url, stdout="", stderr="", status=""):
        logs.append({"status": status, "stderr": stderr})

    monkeypatch.setattr(se, "_append_to_script_log", _fake_log)

    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None

    # Quiesce any residual abandoned workers from prior tests, then install a
    # tracking semaphore seeded at the current real value (which must be MAX).
    real = se._HTTP_INFLIGHT
    deadline = time.monotonic() + 8.0
    while real._value < se.MAX_HTTP_DISPATCH_THREADS and time.monotonic() < deadline:
        time.sleep(0.05)
    tracker = _TrackingSemaphore(se.MAX_HTTP_DISPATCH_THREADS)
    monkeypatch.setattr(se, "_HTTP_INFLIGHT", tracker)
    yield tracker, logs
    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None


def _wait_value(sem: threading.BoundedSemaphore, target: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sem._value >= target:
            return sem._value
        time.sleep(0.05)
    return sem._value


# --------------------------------------------------------------------------
# 40+ concurrent dispatches mixing fast / RST / dribble(abandoned): after every
# dispatch settles, the tracking semaphore returns to MAX (no leak), it never
# over-releases (no double-release ValueError), and it never went below 0.
# --------------------------------------------------------------------------
def test_concurrent_chaos_conserves_permits(_chaos_env):
    tracker, _logs = _chaos_env
    MAX = se.MAX_HTTP_DISPATCH_THREADS

    with (
        _ChaosServer("fast") as fast,
        _ChaosServer("rst") as rst,
        _ChaosServer("dribble", dribble_interval=0.1) as drib,
    ):
        urls = []
        # 16 fast, 12 rst, 16 dribble == 44 concurrent dispatches > MAX(32),
        # so some are non-blocking dropped; all must still conserve the pool.
        for _ in range(16):
            urls.append((f"http://127.0.0.1:{fast.port}/", 1.0))
        for _ in range(12):
            urls.append((f"http://127.0.0.1:{rst.port}/", 1.0))
        for _ in range(16):
            urls.append((f"http://127.0.0.1:{drib.port}/", 0.5))

        threads: list[threading.Thread] = []
        for url, tmo in urls:

            def _drive(u=url, t=tmo):
                with contextlib.suppress(BaseException):
                    se._dispatch_http_request(
                        u, "{}", {"Content-Type": "application/json"}, t, "post-install", u
                    )

            th = threading.Thread(target=_drive, daemon=True)
            threads.append(th)

        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=8.0)
        alive = [t for t in threads if t.is_alive()]
        assert not alive, f"{len(alive)} dispatch caller(s) hung past their deadline"

        recovered = _wait_value(tracker, MAX, timeout=8.0)

    assert tracker.release_errors == [], (
        f"BoundedSemaphore over-release (double-release) detected: {tracker.release_errors}"
    )
    assert tracker.min_seen >= 0, f"permit value went negative ({tracker.min_seen})"
    assert recovered == MAX, (
        f"permit pool not conserved under chaos: value={recovered}, expected {MAX} "
        "-- a leaked (never-released) permit on a fast/RST/abandoned path"
    )


# --------------------------------------------------------------------------
# Serial interleave: fast -> dribble(abandoned) -> rst -> fast, repeated. After
# EACH dispatch the pool returns to MAX, so no failure mode pins a permit.
# --------------------------------------------------------------------------
def test_serial_interleave_each_cycle_returns_to_max(_chaos_env):
    tracker, _logs = _chaos_env
    MAX = se.MAX_HTTP_DISPATCH_THREADS

    with (
        _ChaosServer("fast") as fast,
        _ChaosServer("rst") as rst,
        _ChaosServer("dribble", dribble_interval=0.1) as drib,
    ):
        plan = [
            (f"http://127.0.0.1:{fast.port}/", 1.0),
            (f"http://127.0.0.1:{drib.port}/", 0.5),
            (f"http://127.0.0.1:{rst.port}/", 1.0),
            (f"http://127.0.0.1:{fast.port}/", 1.0),
        ]
        for i, (url, tmo) in enumerate(plan * 2):
            t0 = time.monotonic()
            se._dispatch_http_request(
                url, "{}", {"Content-Type": "application/json"}, tmo, "post-install", url
            )
            assert time.monotonic() - t0 < 4.0, f"dispatch {i} hung on {url}"
            recovered = _wait_value(tracker, MAX, timeout=5.0)
            assert recovered == MAX, (
                f"after dispatch {i} ({url}) pool={recovered}, expected {MAX} "
                "-- this failure mode pinned/leaked a permit"
            )

    assert tracker.release_errors == [], f"over-release detected: {tracker.release_errors}"
