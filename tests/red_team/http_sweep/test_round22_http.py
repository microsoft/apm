"""Round-22 red-team: ABANDONED-DAEMON LEAK AMPLIFICATION + r21 proxy/edge.

These probes drive the REAL ``apm_cli.core.script_executors`` dispatch and
assert observed wall-clock / live-thread facts (never URL substrings).

Primary pivot -- the round-21 slow-loris fix. Round-21 made
``_dispatch_http_request`` enforce a TOTAL wall-clock deadline by running
``requests.post`` on an INNER daemon worker (name ``apm-http-post``) and
``worker.join(total_deadline)``; past the deadline the still-reading daemon
is ABANDONED and the dispatch returns. Round-21 ACCEPTED, as a MED residual,
that ONE abandoned daemon's socket is reaped when the short-lived install
process exits.

The genuine break this round: that residual AMPLIFIES. ``dispatch_http_batch``
drains ALL N http entries through a pool of <= ``MAX_HTTP_DISPATCH_THREADS``
(32) workers, and each pool worker loops ``_dispatch_http_request`` per entry
(see ``_worker``: ``while True: work.get_nowait(); _dispatch_http_request(...)``).
The 32-cap bounds POOL-worker concurrency -- it does NOT bound the number of
ABANDONED ``apm-http-post`` daemons, because each slow entry leaks ONE more
daemon+socket and the pool worker immediately moves to the next entry. A
single pool worker draining a queue of N attacker-authored slow http entries
therefore leaks O(N) live daemons + O(N) open sockets/fds, all accumulating
WITHIN one ``apm install`` run. An attacker controls N (apm.yml has no cap on
the number of lifecycle http entries), so N in the thousands exhausts the
process fd table before the install ever exits.

``test_abandoned_daemon_leak_amplifies_serially`` reproduces this by calling
``_dispatch_http_request`` serially against a CONCURRENT dribble server --
exactly the inner-loop body of one pool ``_worker`` -- and snapshotting the
live ``apm-http-post`` daemon count: it grows linearly with N and far past
any constant bound.
"""

from __future__ import annotations

import contextlib
import math
import socket
import threading
import time

import pytest

from apm_cli.core import script_executors as se

# Captured before the http_sweep conftest's autouse ``hermetic_dns`` fixture
# patches ``socket.getaddrinfo`` to a fixed public IP; restored per-test so a
# REAL loopback listener resolves 127.0.0.1 truthfully.
_REAL_GETADDRINFO = socket.getaddrinfo


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ConcurrentDribbleServer:
    """Loopback server that dribbles forever on EVERY connection at once.

    Unlike the round-21 single-connection harness, this spawns a daemon
    handler thread per accepted connection, so many leaked client daemons
    can each stay blocked on a live, never-completing read simultaneously --
    the condition that makes the abandoned-daemon leak observable.
    """

    def __init__(self, dribble_interval: float, lifetime: float = 30.0) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(256)
        self._sock.settimeout(2.0)
        self.stop = threading.Event()
        self.accepted = 0
        self._interval = dribble_interval
        self._lifetime = lifetime
        self._t = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                if self.stop.is_set():
                    return
                continue
            self.accepted += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn) -> None:
        try:
            conn.settimeout(2.0)
            # best-effort drain of the request headers
            with contextlib.suppress(OSError):
                conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            deadline = time.monotonic() + self._lifetime
            while not self.stop.is_set() and time.monotonic() < deadline:
                try:
                    # never emit the terminating CRLFCRLF: client stays in
                    # header-read forever, resetting its per-recv timeout.
                    conn.sendall(b"X")
                except OSError:
                    return
                time.sleep(self._interval)
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()


@pytest.fixture(autouse=True)
def _direct_path_real_dns(monkeypatch):
    """Route dispatch through the bare ``requests.post`` direct path.

    The guarded DNS-pinned session refuses loopback (loopback == internal),
    correct in production but blocking for a local timing harness. Returning
    ``None`` exercises the SAME requests-call configuration the production
    dispatch builds. Also restore the real resolver the conftest stubbed.
    """
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    se._GUARDED_SESSION = None
    yield
    se._GUARDED_SESSION = None


def _live_post_daemons() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "apm-http-post" and t.is_alive())


def _settle_baseline(max_wait: float = 6.0) -> int:
    """Wait for any abandoned daemons from a PRIOR test to drain, then return
    the stable live ``apm-http-post`` count to use as a clean baseline.

    Prior probes intentionally leak daemons; once their server stops those
    daemons get EOF and exit, but not instantly. Polling until the count is
    non-increasing avoids a prior test's dying daemons skewing this test's
    before/after delta.
    """
    deadline = time.monotonic() + max_wait
    prev = _live_post_daemons()
    stable = 0
    while time.monotonic() < deadline:
        time.sleep(0.3)
        cur = _live_post_daemons()
        if cur <= prev:
            stable += 1
            if stable >= 3 and cur == prev:
                return cur
        else:
            stable = 0
        prev = cur
    return _live_post_daemons()


# --------------------------------------------------------------------------
# PRIMARY BREAK: abandoned-daemon leak amplifies linearly with attacker N.
#
# Mirror one pool ``_worker`` draining a queue of N slow http entries: call
# ``_dispatch_http_request`` serially N times against a concurrent dribbler.
# Each call returns after ~total_deadline (the inner daemon is abandoned,
# still holding its socket). The live ``apm-http-post`` daemon count must NOT
# be bounded by a small constant -- if it tracks N, the 32-worker pool cap is
# defeated and a large-N apm.yml exhausts fds within one install.
# --------------------------------------------------------------------------
def test_abandoned_daemon_leak_amplifies_serially():
    dispatch_timeout = 0.5  # attacker timeoutSec -> total_deadline 0.5s
    n_entries = 22
    # dribble well under the per-recv read timeout so each abandoned daemon
    # keeps its socket alive past abandonment.
    server = _ConcurrentDribbleServer(dribble_interval=dispatch_timeout * 0.4, lifetime=30.0)

    with server:
        url = f"http://127.0.0.1:{server.port}/"
        before = _settle_baseline()
        start = time.monotonic()
        for _ in range(n_entries):
            # exactly the body of dispatch_http_batch's _worker loop
            se._dispatch_http_request(
                url,
                "{}",
                {"Content-Type": "application/json"},
                dispatch_timeout,
                "post-install",
                url,
            )
        drain_wall = time.monotonic() - start
        # let the scheduler settle; daemons are still mid-read (dribbling).
        time.sleep(0.3)
        leaked = _live_post_daemons() - before
        server.stop.set()

    # Each serial dispatch returned promptly (total deadline held): the whole
    # drain must be ~n_entries * total_deadline, not n_entries * lifetime.
    assert drain_wall < n_entries * dispatch_timeout * 2.0, (
        f"serial drain took {drain_wall:.1f}s; dispatch did not honor the total deadline"
    )
    # The leak is the break: live abandoned daemons track N, not the 32 cap
    # nor any small constant. >= 16 of 22 proves linear-in-N accumulation
    # within a SINGLE drain (one pool worker), i.e. fd exhaustion at large N.
    assert leaked >= 16, (
        f"expected ~{n_entries} leaked apm-http-post daemons (linear in "
        f"attacker-controlled N); observed {leaked}. If this is a small "
        f"constant the leak is bounded and not a break."
    )


# --------------------------------------------------------------------------
# SECONDARY: the pool cap does NOT cap leaked sockets. Run the REAL
# dispatch_http_batch-style worker loop and confirm leaked daemons exceed
# MAX_HTTP_DISPATCH_THREADS is *possible* by construction. We assert the
# weaker, robust fact: a single drain leaks > the constant the round-21
# residual implied (one).
# --------------------------------------------------------------------------
def test_leak_exceeds_constant_residual():
    dispatch_timeout = 0.4
    n_entries = 14
    server = _ConcurrentDribbleServer(dribble_interval=dispatch_timeout * 0.4, lifetime=30.0)
    with server:
        url = f"http://127.0.0.1:{server.port}/"
        before = _settle_baseline()
        for _ in range(n_entries):
            se._dispatch_http_request(
                url,
                "{}",
                {"Content-Type": "application/json"},
                dispatch_timeout,
                "post-install",
                url,
            )
        time.sleep(0.3)
        leaked = _live_post_daemons() - before
        server.stop.set()
    # Round-21 framed the residual as "a single abandoned daemon". Here many
    # accumulate from one drain -- the residual is not single.
    assert leaked > 5, f"only {leaked} leaked; residual would need to be ~1 to be benign"


# --------------------------------------------------------------------------
# DEADLINE EDGE: a non-finite / huge / exactly-30 timeoutSec must clamp to a
# finite ceiling so the inner-worker join is always bounded.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [float("inf"), float("nan"), 1e18, 30.0, 29.999999, -5.0, 0.0],
)
def test_deadline_always_finite_and_bounded(raw):
    out = se._coerce_http_deadline(raw)
    assert math.isfinite(out)
    assert 0 < out <= se._MAX_HTTP_TIMEOUT


# --------------------------------------------------------------------------
# PROXY GATE: the destination SSRF gate runs whether or not an env proxy is
# configured. A NO_PROXY / proxy env must not let an internal destination
# slip through _prepare_http (the prep-time gate is proxy-agnostic).
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "127.0.0.1", "10.0.0.5", "metadata.google.internal", "[::1]"],
)
def test_ssrf_gate_holds_under_proxy_env(monkeypatch, host):
    # An attacker-influenced proxy environment must not relax the gate.
    monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "*")
    # restore real resolver so the literal/metadata classification is honest
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    reason = se._ssrf_block_reason(host.strip("[]"))
    assert reason is not None, f"internal host {host} not blocked under proxy env"


# --------------------------------------------------------------------------
# IPv6 ZONE-ID / SCOPED: a scoped literal (fe80::1%eth0, ::1%lo) must still be
# classified internal, not slip past _ip_is_internal via the zone suffix.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host",
    ["fe80::1%eth0", "::1%lo0", "fe80::1", "::1", "fc00::1", "fec0::1"],
)
def test_ipv6_scoped_internal_blocked(monkeypatch, host):
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    reason = se._ssrf_block_reason(host)
    assert reason is not None, f"scoped/internal IPv6 {host} not blocked"
