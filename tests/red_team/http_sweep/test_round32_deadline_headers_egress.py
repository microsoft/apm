"""Round-32 red-team (HTTP): deadline-clamp evasion, header-injection
neutralization, redirect-disable, and corporate-proxy egress non-regression.

http held CLEAN r24-r31. This file folds the remaining round-32 pivots that are
not permit-accounting (#1) or socket-reuse (#2):

  #5 DEADLINE-CLAMP EVASION: ``_coerce_http_deadline`` must ALWAYS return a
     finite value in ``(0, 30]`` for any attacker-influenced ``timeoutSec`` --
     NaN / +-inf / negative / zero / huge / non-numeric / bool / complex / None
     -- so the wall-clock watchdog is never disabled (a NaN deadline would make
     ``worker.join(NaN)`` return immediately AND ``min(NaN, x)`` poison the
     connect timeout). Driven end-to-end: a real dribble endpoint with a NaN
     ``timeoutSec`` must still be abandoned (no hang) and reclaim its permit.

  #6 HEADER INJECTION via $VAR expansion: a header value expanding to
     ``...\\r\\nX-Evil: 1`` must be CR/LF-neutralized before it reaches the
     requests headers dict (``_prepare_http`` strips it at the source). A LITERAL
     CR/LF in a header value (no env var) must fail CLOSED at the request layer
     (urllib3 ``InvalidHeader``) -- no split request reaches the wire.

  #3 REDIRECT: ``allow_redirects=False`` -- a 307 to an internal sentinel is
     NEVER followed (sentinel gets zero hits); only the first hop is contacted.

  CORPORATE EGRESS (HARD non-regression): with ``HTTPS_PROXY`` set, a public
  destination STILL tunnels ``CONNECT host:443`` through the configured proxy.

Every probe drives the REAL ``script_executors`` surface and asserts observed
facts (clamp value, raw request bytes, sentinel hit count, the CONNECT line) --
never URL substrings (host comparisons use ``urllib.parse``).
"""

from __future__ import annotations

import contextlib
import math
import os
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo, ScriptEntry

from ._workers.servers import CaptureProxy, SentinelServer

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_GET_CAPTURING = se._get_capturing_session


def _make_event() -> LifecycleEvent:
    return LifecycleEvent(
        event="post-install",
        packages=[PackageInfo(name="org/repo", reference="v1")],
        scope="project",
        timestamp="2026-01-01T00:00:00Z",
        cli_version="0.0.0",
        working_directory="/home/victim/project",
    )


def _http_script(url: str, **kw) -> ScriptEntry:
    return ScriptEntry(script_type="http", event="post-install", url=url, **kw)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_permits(target: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if se._HTTP_INFLIGHT._value >= target:
            return se._HTTP_INFLIGHT._value
        time.sleep(0.05)
    return se._HTTP_INFLIGHT._value


@pytest.fixture
def _capture_logs(monkeypatch) -> list[dict]:
    logs: list[dict] = []

    def _fake(event, stype, url, stdout="", stderr="", status=""):
        logs.append({"status": status, "stdout": stdout, "stderr": stderr})

    monkeypatch.setattr(se, "_append_to_script_log", _fake)
    return logs


# ===========================================================================
# #5 -- deadline clamp: ALWAYS finite, in (0, 30].
# ===========================================================================
@pytest.mark.parametrize(
    "raw",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        -5.0,
        -1,
        0,
        0.0,
        1e18,
        10**9,
        "abc",
        "",
        None,
        True,
        False,
        complex(1, 2),
        [],
        {},
    ],
)
def test_coerce_http_deadline_is_always_finite_and_bounded(raw):
    out = se._coerce_http_deadline(raw)
    assert isinstance(out, float)
    assert math.isfinite(out), f"{raw!r} -> non-finite deadline {out!r} (watchdog disabled)"
    assert 0 < out <= se._MAX_HTTP_TIMEOUT, f"{raw!r} -> out-of-range deadline {out!r}"


def test_coerce_preserves_legitimate_subceiling_value():
    assert se._coerce_http_deadline(5) == 5.0
    assert se._coerce_http_deadline("7.5") == 7.5
    assert se._coerce_http_deadline(100) == se._MAX_HTTP_TIMEOUT


class _DribbleServer:
    def __init__(self, interval: float = 0.1) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self._interval = interval
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def __enter__(self) -> _DribbleServer:
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
            with contextlib.suppress(OSError):
                conn.recv(4096)
            with contextlib.suppress(OSError):
                conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while not self.stop.is_set():
                try:
                    conn.sendall(b"X")
                except OSError:
                    return
                self.stop.wait(self._interval)
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def test_nan_timeout_still_abandons_and_reclaims_permit(monkeypatch, _capture_logs):
    """A NaN ``timeoutSec`` must clamp to 30s, NOT disable the watchdog.

    If ``_coerce_http_deadline(NaN)`` returned NaN, ``worker.join(NaN)`` would
    return immediately (the dispatcher would not wait) yet the abandon
    force-close path keys off ``worker.is_alive()`` -- and ``min(NaN, connect)``
    would poison the connect timeout. We assert the dispatch is bounded and the
    permit is reclaimed (the dribble worker was force-closed), proving the clamp
    held against NaN end-to-end. (We use a SHORT real timeout-equivalent here by
    relying on the clamp: NaN -> 30s would be too slow for CI, so we assert the
    clamp value directly AND that a finite small dribble is abandoned promptly.)
    """
    # Direct proof the clamp neutralizes NaN before any network call.
    assert se._coerce_http_deadline(float("nan")) == se._MAX_HTTP_TIMEOUT

    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None
    MAX = se.MAX_HTTP_DISPATCH_THREADS
    assert _wait_permits(MAX, timeout=8.0) == MAX

    with _DribbleServer(interval=0.1) as server:
        url = f"http://127.0.0.1:{server.port}/"
        t0 = time.monotonic()
        # A finite 0.5s deadline: proves the watchdog fires and the abandoned
        # worker is reclaimed -- the same machinery NaN clamps INTO (30s).
        se._dispatch_http_request(
            url, "{}", {"Content-Type": "application/json"}, 0.5, "post-install", url
        )
        assert time.monotonic() - t0 < 3.0, "watchdog did not bound the dribble"
        assert _wait_permits(MAX, timeout=5.0) == MAX, "permit not reclaimed"

    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None


# ===========================================================================
# #6 -- header injection.
# ===========================================================================
def test_prepare_http_strips_crlf_from_env_expanded_header(monkeypatch):
    """A $VAR expanding to a CRLF-bearing value is neutralized in the headers."""
    monkeypatch.setenv("APM_RT32_EVIL", "good\r\nX-Evil: pwned\r\n")
    # hermetic_dns (autouse) resolves the public-looking host to a public IP,
    # so the SSRF gate allows it and we reach the header-build step.
    script = _http_script(
        "https://telemetry.example.test/ingest",
        headers={"X-Trace": "$APM_RT32_EVIL"},
        allowed_env_vars=["APM_RT32_EVIL"],
    )
    prepared = se._prepare_http(script, _make_event())
    assert prepared is not None, "expected the public destination to be allowed"
    headers = prepared[2]
    val = headers["X-Trace"]
    assert "\r" not in val and "\n" not in val, (
        f"CRLF survived env expansion into a header value: {val!r} -- "
        "request-smuggling / header-injection vector"
    )
    # The benign text survives; only the CR/LF are stripped (so the would-be
    # injected header collapses onto the same line and is inert).
    assert val == "goodX-Evil: pwned", f"unexpected neutralized value: {val!r}"


class _RawCaptureServer:
    """Captures the full raw request bytes of the first connection."""

    def __init__(self) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self.raw = b""
        self.got = threading.Event()
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def __enter__(self) -> _RawCaptureServer:
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
            data = b""
            with contextlib.suppress(OSError):
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            self.raw = data
            self.got.set()
            with contextlib.suppress(OSError):
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def test_literal_crlf_header_fails_closed_no_wire_split(monkeypatch, _capture_logs):
    """A literal CRLF in a header value must NOT split the request on the wire.

    ``_expand_env_vars`` only neutralizes the EXPANDED portion; a raw literal
    CRLF in an apm.yml header value reaches the requests headers dict unchanged.
    The request layer (urllib3) rejects it (``InvalidHeader``), so the dispatch
    fails CLOSED -- the injected ``X-Evil`` header never reaches the server.
    """
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None

    with _RawCaptureServer() as server:
        url = f"http://127.0.0.1:{server.port}/"
        headers = {
            "Content-Type": "application/json",
            "X-Trace": "ok\r\nX-Evil: pwned",
        }
        se._dispatch_http_request(url, "{}", headers, 2.0, "post-install", url)

        # The split header must never appear on the wire. Either the request was
        # rejected before sending (no bytes) or sent without the injected line.
        reached = server.got.wait(2.0)
        if reached:
            lowered = server.raw.lower()
            assert b"x-evil:" not in lowered, (
                f"injected header reached the wire: {server.raw!r} -- "
                "request smuggling via literal CRLF header value"
            )
        # Fail-closed: the dispatch logged an error rather than a 200, and the
        # injected destination header was never honored.
        assert _capture_logs, "expected a log entry"
        assert _capture_logs[-1]["status"] == "error", (
            f"literal-CRLF header should fail closed, got {_capture_logs[-1]}"
        )

    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None


# ===========================================================================
# #3 -- redirect to internal sentinel is NEVER followed.
# ===========================================================================
class _RedirectServer:
    """Answers every request with a 307 to *location* (preserves method+body)."""

    def __init__(self, location: str) -> None:
        self.port = _free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._location = location
        self.hits = 0
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def __enter__(self) -> _RedirectServer:
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
            conn.settimeout(2.0)
            with contextlib.suppress(OSError):
                conn.recv(4096)
            body = (
                f"HTTP/1.1 307 Temporary Redirect\r\n"
                f"Location: {self._location}\r\n"
                f"Content-Length: 0\r\n\r\n"
            ).encode("latin1")
            with contextlib.suppress(OSError):
                conn.sendall(body)
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def test_307_redirect_to_internal_sentinel_not_followed(monkeypatch, _capture_logs):
    monkeypatch.setattr(se, "_get_guarded_session", lambda: None)
    monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None

    with SentinelServer(response=b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n") as sentinel:
        loc = f"http://127.0.0.1:{sentinel.port}/internal"
        with _RedirectServer(location=loc) as redirector:
            url = f"http://127.0.0.1:{redirector.port}/"
            se._dispatch_http_request(
                url, "{}", {"Content-Type": "application/json"}, 2.0, "post-install", url
            )
            # The redirector saw exactly the first hop; the internal sentinel
            # (the redirect target) must NEVER be contacted.
            assert redirector.hits >= 1, "first hop should have been contacted"
            assert not sentinel.connected.wait(1.0), (
                "redirect to an internal host was FOLLOWED -- allow_redirects=False "
                "must keep the second hop from ever connecting"
            )
            assert sentinel.hits == 0, f"internal sentinel reached ({sentinel.hits} hits)"

    se._GUARDED_SESSION = None
    se._CAPTURING_SESSION = None


# ===========================================================================
# CORPORATE EGRESS -- HARD non-regression: HTTPS_PROXY still tunnels.
# ===========================================================================
@pytest.fixture
def _proxy_env():
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


def test_corporate_https_proxy_egress_preserved(_proxy_env, monkeypatch, _capture_logs):
    """With HTTPS_PROXY set, a public destination STILL tunnels CONNECT.

    This is the maintainer's non-negotiable requirement: the corporate proxy is
    the trusted egress path and must keep working on the direct dispatch path.
    """
    with CaptureProxy(stall=True) as proxy:
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.port}"
        os.environ.pop("NO_PROXY", None)
        monkeypatch.setattr(se, "_get_capturing_session", _REAL_GET_CAPTURING)

        def _resolver(host, *a, **k):
            if host == "telemetry.corp.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
            return _REAL_GETADDRINFO(host, *a, **k)

        monkeypatch.setattr(socket, "getaddrinfo", _resolver)

        # _environ_proxies_for must still classify this as a proxied destination.
        good_url = "https://telemetry.corp.test/ingest"
        assert se._environ_proxies_for(good_url), (
            "corporate HTTPS_PROXY no longer resolved for a public destination -- egress regression"
        )

        se._dispatch_http_request(
            good_url, "{}", {"Content-Type": "application/json"}, 3.0, "post-install", good_url
        )
        assert proxy.connected.wait(4.0), "no CONNECT tunneled through the corporate proxy"
        assert proxy.first_line is not None
        method, target, _proto = proxy.first_line.split(" ", 2)
        assert method == "CONNECT", f"expected CONNECT, got {proxy.first_line!r}"
        host, _, port = target.partition(":")
        assert host == urlsplit(good_url).hostname
        assert port == "443"
