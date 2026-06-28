"""Round-21 red-team: RESOURCE BOUNDS + remaining SSRF edges.

Each probe drives the REAL ``apm_cli.core.script_executors`` dispatch
against a local loopback server (stdlib sockets in daemon threads bound
to 127.0.0.1:0). Behaviour is proven by observed wall-clock / connection
facts -- never URL substring checks.

Pivot focus (round-20 primed):
  * RESPONSE-SIZE  -- is the body ever pulled into memory? (round-2 / r20
    cover this; re-confirmed here against a REAL huge-body server.)
  * SLOW-LORIS     -- the single-float ``timeout`` is a PER-RECV socket
    deadline, NOT a total deadline. round-20 only proved a SILENT server
    trips the read timeout. The genuine gap is a DRIBBLE server that
    sends one byte just under the per-recv interval forever: it resets
    the socket timeout on every recv, so the dispatch thread hangs PAST
    the configured ``timeout`` with no total bound.
  * REDIRECT       -- ``allow_redirects=False`` must hold per real server.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time

import pytest

from apm_cli.core import script_executors as se

# Captured before the http_sweep conftest's autouse ``hermetic_dns``
# fixture patches ``socket.getaddrinfo`` to a fixed public IP on port 0.
# These probes use a REAL loopback listener, so they must resolve
# 127.0.0.1 truthfully; the fixture below restores this genuine resolver.
_REAL_GETADDRINFO = socket.getaddrinfo


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _LoopbackServer:
    """Single-connection loopback HTTP server with scriptable behaviour."""

    def __init__(self, handler) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(4)
        self._sock.settimeout(8.0)
        self.stop = threading.Event()
        self.accepted = 0
        self.target_connects = 0
        self._handler = handler
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            self.accepted += 1
            try:
                self._handler(self, conn)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    conn.close()

    def __exit__(self, *exc) -> None:
        self.stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()


def _read_request(conn) -> bytes:
    conn.settimeout(3.0)
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    return data


@pytest.fixture(autouse=True)
def _force_bare_requests(monkeypatch):
    """Route the dispatch through the bare ``requests.post`` fallback.

    The guarded DNS-pinned session refuses loopback (loopback == internal),
    which is correct but blocks a local timing harness. The bare fallback
    uses the SAME requests-call configuration the production dispatch
    builds (scalar ``timeout``, ``stream=True``, ``allow_redirects=False``,
    explicit ``proxies``); returning ``None`` exercises that real path
    against a controllable local endpoint. We are probing the TIMEOUT /
    REDIRECT / BODY semantics of the request call, which are orthogonal to
    WHERE the pin lets us connect (a real attacker uses a public host that
    dribbles, modelled here by a loopback dribbler).
    """
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    # The http_sweep conftest's autouse ``hermetic_dns`` fixture patches
    # ``socket.getaddrinfo`` to return a fixed PUBLIC IP on port 0, which
    # would misdirect our loopback connections and fail them fast (masking
    # the real timing). Restore the genuine resolver so 127.0.0.1 connects.
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    se._GUARDED_SESSION = None
    yield
    se._GUARDED_SESSION = None


def _dispatch_in_thread(url: str, timeout: float) -> threading.Thread:
    th = threading.Thread(
        target=se._dispatch_http_request,
        kwargs=dict(
            url=url,
            payload="{}",
            request_headers={"Content-Type": "application/json"},
            timeout=timeout,
            event_name="post-install",
            safe_url=url,
        ),
        daemon=True,
    )
    th.start()
    return th


# --------------------------------------------------------------------------
# CONTROL: a SILENT server (accepts, sends nothing) must trip the per-recv
# read timeout within ~timeout. This is the case round-20 already proved.
# --------------------------------------------------------------------------
def test_silent_server_trips_read_timeout():
    def _handler(srv, conn):
        _read_request(conn)
        # send nothing; hold open until torn down
        while not srv.stop.is_set():
            time.sleep(0.05)

    with _LoopbackServer(_handler) as srv:
        url = f"http://127.0.0.1:{srv.port}/"
        th = _dispatch_in_thread(url, timeout=1.0)
        th.join(timeout=4.0)
        alive = th.is_alive()
    assert not alive, "silent server should trip the per-recv read timeout (~1s)"


# --------------------------------------------------------------------------
# SLOW-LORIS: a DRIBBLE server that emits one header byte just under the
# per-recv interval resets the socket timeout on every recv. With only a
# scalar (per-recv) timeout and NO total deadline, the dispatch thread
# hangs PAST the configured timeout. SECURE CONTRACT: the dispatch must be
# bounded by a TOTAL deadline so it cannot be held open beyond ~timeout.
# --------------------------------------------------------------------------
def test_dribble_slowloris_hangs_past_timeout():
    dispatch_timeout = 1.0

    def _handler(srv, conn):
        _read_request(conn)
        try:
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            # Dribble header bytes one at a time, each well within the
            # per-recv timeout, for far longer than ``dispatch_timeout``.
            # Never complete the header block (no final CRLFCRLF).
            deadline = time.monotonic() + 8.0
            while not srv.stop.is_set() and time.monotonic() < deadline:
                try:
                    conn.sendall(b"X")
                except OSError:
                    return
                # sleep < dispatch_timeout so each recv resets the deadline
                time.sleep(dispatch_timeout * 0.5)
        except OSError:
            return

    with _LoopbackServer(_handler) as srv:
        url = f"http://127.0.0.1:{srv.port}/"
        start = time.monotonic()
        th = _dispatch_in_thread(url, timeout=dispatch_timeout)
        # Wait ~3.5x the configured timeout. A correctly TOTAL-bounded
        # dispatch would have returned by ~timeout; a per-recv-only bound
        # leaves the thread alive (still dribbling).
        th.join(timeout=dispatch_timeout * 3.5)
        alive = th.is_alive()
        waited = time.monotonic() - start
        srv.stop.set()

    # SECURE CONTRACT (asserted): no total deadline is missing -> the
    # dispatch must NOT still be alive well past its configured timeout.
    # This FAILS today: the single-float timeout is per-recv only.
    assert not alive, (
        "SLOW-LORIS: dispatch thread is STILL ALIVE "
        f"{waited:.1f}s after start with timeout={dispatch_timeout}s. "
        "A dribbling server resets the per-recv socket timeout on every "
        "byte; requests' single-float timeout has NO total deadline, so "
        "the in-process http dispatch can be held open indefinitely past "
        "its configured timeout (resource/thread/fd hold)."
    )


# --------------------------------------------------------------------------
# REDIRECT: a 302 to an internal/metadata host must NOT be followed. The
# dispatch sets allow_redirects=False -- confirm the redirect target is
# never dialled (no second connection to the internal listener).
# --------------------------------------------------------------------------
def test_redirect_to_internal_not_followed():
    internal_hits = {"n": 0}

    # Internal listener standing in for 169.254.169.254 metadata.
    def _internal_handler(srv, conn):
        internal_hits["n"] += 1
        _read_request(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

    with _LoopbackServer(_internal_handler) as internal:
        redirect_target = f"http://127.0.0.1:{internal.port}/latest/meta-data/"

        def _redir_handler(srv, conn):
            _read_request(conn)
            conn.sendall(
                b"HTTP/1.1 302 Found\r\n"
                + b"Location: "
                + redirect_target.encode()
                + b"\r\nContent-Length: 0\r\n\r\n"
            )

        with _LoopbackServer(_redir_handler) as redir:
            url = f"http://127.0.0.1:{redir.port}/"
            th = _dispatch_in_thread(url, timeout=2.0)
            th.join(timeout=5.0)
            alive = th.is_alive()
            # give any (erroneous) redirect a beat to land
            time.sleep(0.3)

    assert not alive, "dispatch should complete on a 302 (no body, fast)"
    assert internal_hits["n"] == 0, (
        "REDIRECT-SSRF: dispatch followed a 302 to the internal metadata "
        f"listener 127.0.0.1:{internal.port} ({internal_hits['n']} hit). "
        "allow_redirects must stay False so a public->internal 302 cannot "
        "pivot past the per-hop SSRF gate."
    )


# --------------------------------------------------------------------------
# RESPONSE-SIZE: a REAL server advertising a huge body must not OOM the
# dispatch. stream=True + status-only read => return is fast and the giant
# body is never pulled into memory. Proven by wall-clock (no GB read).
# --------------------------------------------------------------------------
def test_huge_body_not_buffered():
    chunk = b"A" * 65536
    huge_len = 4 * 1024 * 1024 * 1024  # 4 GiB advertised

    def _handler(srv, conn):
        _read_request(conn)
        try:
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n" + f"Content-Length: {huge_len}\r\n".encode() + b"\r\n"
            )
            # stream body until the client goes away (it should, fast).
            sent = 0
            while not srv.stop.is_set() and sent < huge_len:
                try:
                    conn.sendall(chunk)
                except OSError:
                    return
                sent += len(chunk)
        except OSError:
            return

    with _LoopbackServer(_handler) as srv:
        url = f"http://127.0.0.1:{srv.port}/"
        start = time.monotonic()
        th = _dispatch_in_thread(url, timeout=3.0)
        th.join(timeout=6.0)
        elapsed = time.monotonic() - start
        alive = th.is_alive()
        srv.stop.set()

    assert not alive, "dispatch hung on a huge-body server"
    # Reading 4 GiB at any realistic loopback rate would take many seconds;
    # a fast return proves the body was never consumed into memory.
    assert elapsed < 5.0, (
        f"dispatch took {elapsed:.1f}s on a 4 GiB body -- suggests the body "
        "was being buffered/read instead of left on the wire (OOM risk)."
    )
