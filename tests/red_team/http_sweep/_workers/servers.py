"""Round-31 red-team helper workers (real loopback sentinels / proxies).

Pure-stdlib socket servers bound to ``127.0.0.1`` used to drive the REAL
``apm_cli.core.script_executors`` dispatch seam. No guard logic is
reimplemented here -- these are only network counterparts: a sentinel that
counts connections, a CONNECT-capturing proxy, a redirector, a header-phase
dribbler.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SentinelServer:
    """Real TCP listener on 127.0.0.1 that COUNTS accepted connections.

    Models an 'internal' SSRF target: a secure executor must connect ZERO
    times. Optionally serves a fixed first response then closes, stalls
    (dribble) for a bounded hold, or RSTs mid-handshake.
    """

    def __init__(
        self,
        *,
        response: bytes | None = None,
        stall: bool = False,
        hold: float = 20.0,
        reset: bool = False,
    ) -> None:
        self.port = free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(32)
        self._sock.settimeout(0.5)
        self._response = response
        self._stall = stall
        self._hold = hold
        self._reset = reset
        self.hits = 0
        self.first_line: str | None = None
        self.connected = threading.Event()
        self.stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> SentinelServer:
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
            if self._reset:
                with contextlib.suppress(OSError):
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return
            if self._stall:
                self.stop.wait(self._hold)
                return
            if self._response is not None:
                with contextlib.suppress(OSError):
                    conn.sendall(self._response)
        finally:
            with contextlib.suppress(OSError):
                conn.close()


class HeaderDribbleServer:
    """Accept, then dribble response HEADER bytes under the per-recv timeout.

    Models a header-phase slow-loris: one partial status byte then a sleep
    loop. The dispatcher (stream=True) must force-close at the total deadline
    and reclaim the permit. Hold is bounded so server fds clear during settle.
    """

    def __init__(self, *, hold: float = 20.0) -> None:
        self.port = free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(32)
        self._sock.settimeout(0.5)
        self._hold = hold
        self.hits = 0
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def __enter__(self) -> HeaderDribbleServer:
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
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            with contextlib.suppress(OSError):
                conn.recv(8192)
            with contextlib.suppress(OSError):
                conn.sendall(b"HTTP/1.1 ")
            waited = 0.0
            while waited < self._hold and not self.stop.is_set():
                with contextlib.suppress(OSError):
                    conn.sendall(b"X")
                self.stop.wait(0.4)
                waited += 0.4
        finally:
            with contextlib.suppress(OSError):
                conn.close()


class CaptureProxy:
    """Real CONNECT-capturing proxy on 127.0.0.1.

    Records the first request line of each connection (the CONNECT line for an
    HTTPS tunnel). Does NOT complete the tunnel -- enough to prove WHETHER a
    CONNECT was emitted and to WHICH host:port.
    """

    def __init__(self, *, stall: bool = False, hold: float = 20.0) -> None:
        self.port = free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(32)
        self._sock.settimeout(0.5)
        self._stall = stall
        self._hold = hold
        self.first_line: str | None = None
        self.connected = threading.Event()
        self.hits = 0
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def __enter__(self) -> CaptureProxy:
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
                self.stop.wait(self._hold)
        finally:
            with contextlib.suppress(OSError):
                conn.close()
