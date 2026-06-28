"""Round-32 red-team (HTTP / socket-reuse): keep-alive after a forced abort.

http held CLEAN r24-r31. Round-32 PIVOT #2: after the dispatcher force-closes
an abandoned slow-loris worker's socket at the total deadline
(``shutdown(SHUT_RDWR)``), is the urllib3 connection POOL left holding a
half-closed socket that a LATER dispatch to the SAME host:port reuses
(keep-alive) and inherits -- a cross-request HANG or contamination?

This probe drives the REAL production capturing session (so connections are
genuinely pooled) against ONE real ``127.0.0.1`` listener whose FIRST
connection dribbles forever (forcing an abandon + force-close) and whose
SUBSEQUENT connections answer a complete 200 immediately. The secure contract:
the second dispatch must NOT hang (it must complete well within its own
deadline) and must observe a clean ``HTTP 200`` -- proving urllib3 discards the
force-closed connection (``is_connection_dropped``) and opens a fresh one rather
than reusing a wedged socket. The ``_HTTP_INFLIGHT`` pool returns to MAX.

No proxy env is set; the direct path is exercised. Corporate egress
non-regression is asserted separately.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time

import pytest

from apm_cli.core import script_executors as se

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_GET_CAPTURING = se._get_capturing_session


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _DribbleThenFastServer:
    """First accepted connection dribbles forever; later ones answer 200 fast.

    Models the keep-alive seam: connection #1 is force-closed by the dispatcher
    at the deadline; connection #2 (a later dispatch to the SAME host:port) must
    get a clean fast response, proving the pool did not hand back the wedged
    socket.
    """

    def __init__(self, dribble_interval: float = 0.1) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self._interval = dribble_interval
        self._n_lock = threading.Lock()
        self.accepted = 0
        self.stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _DribbleThenFastServer:
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
            with self._n_lock:
                self.accepted += 1
                n = self.accepted
            threading.Thread(target=self._handle, args=(conn, n), daemon=True).start()

    def _handle(self, conn: socket.socket, n: int) -> None:
        try:
            conn.settimeout(2.0)
            with contextlib.suppress(OSError):
                conn.recv(4096)
            if n == 1:
                # dribble forever -> client wedged in header-read -> abandoned
                with contextlib.suppress(OSError):
                    conn.sendall(b"HTTP/1.1 200 OK\r\n")
                while not self.stop.is_set():
                    try:
                        conn.sendall(b"X")
                    except OSError:
                        return
                    self.stop.wait(self._interval)
            else:
                with contextlib.suppress(OSError):
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        finally:
            with contextlib.suppress(OSError):
                conn.close()


@pytest.fixture
def _direct_capturing(monkeypatch):
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    logs: list[dict] = []

    def _fake_log(event, stype, url, stdout="", stderr="", status=""):
        logs.append({"status": status, "stdout": stdout, "stderr": stderr})

    monkeypatch.setattr(se, "_append_to_script_log", _fake_log)
    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None
    yield logs
    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None


def _wait_permits(target: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if se._HTTP_INFLIGHT._value >= target:
            return se._HTTP_INFLIGHT._value
        time.sleep(0.05)
    return se._HTTP_INFLIGHT._value


def test_keepalive_after_forced_abort_no_hang_or_contamination(_direct_capturing):
    logs = _direct_capturing
    MAX = se.MAX_HTTP_DISPATCH_THREADS
    assert _wait_permits(MAX, timeout=8.0) == MAX, "semaphore not at rest before test"

    with _DribbleThenFastServer(dribble_interval=0.1) as server:
        url = f"http://127.0.0.1:{server.port}/"

        # Dispatch #1: forced abort at the 0.5s deadline (dribble).
        t0 = time.monotonic()
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 0.5, "post-install", url
        )
        d1 = time.monotonic() - t0
        assert d1 < 3.0, f"first (dribble) dispatch hung {d1:.1f}s"
        assert _wait_permits(MAX, timeout=5.0) == MAX, "permit not reclaimed after abort"
        first_log = logs[-1]
        assert first_log["status"] == "error", "abandoned dribble should log error"

        # Dispatch #2 to the SAME host:port. If urllib3 handed back the
        # force-closed socket from #1, this would hang past its deadline or
        # raise; the secure outcome is a fresh connection -> clean fast 200.
        t1 = time.monotonic()
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 2.0, "post-install", url
        )
        d2 = time.monotonic() - t1
        assert d2 < 2.5, (
            f"second dispatch to same host:port took {d2:.1f}s -- a reused "
            "half-closed keep-alive socket from the forced abort caused a hang"
        )

        second_log = logs[-1]
        assert second_log["status"] == "ok", (
            f"second dispatch did not get a clean response (log={second_log}); "
            "keep-alive reuse of the aborted socket contaminated the request"
        )
        assert "HTTP 200" in second_log.get("stdout", ""), (
            f"expected a clean HTTP 200 on the fresh connection, got {second_log}"
        )
        assert _wait_permits(MAX, timeout=5.0) == MAX, "permit pool not restored"

    # The fast connection that served dispatch #2 must be a DIFFERENT accepted
    # connection than the wedged #1, proving no socket reuse across the abort.
    assert server.accepted >= 2, (
        f"expected >=2 distinct server connections (wedged + fresh), got {server.accepted}"
    )
