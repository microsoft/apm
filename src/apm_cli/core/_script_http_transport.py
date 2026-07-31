"""SSRF guard, session management, and HTTP request dispatch.

Extracted from ``script_executors.py`` to keep that module under the
800-line source budget. All public symbols remain accessible via
``apm_cli.core.script_executors.*`` (re-exported from that module).
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import math
import socket
import threading
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ._script_secret_helpers import (
    _append_to_script_log,
    _expand_env_vars,
    _http_payload,
    _redact_url_credentials,
)

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger
    from apm_cli.core.lifecycle_scripts import LifecycleEvent, ScriptEntry

_logger = logging.getLogger(__name__)

# Hostnames that resolve to cloud-metadata endpoints. Blocked by NAME
# (independent of DNS) because a guard that only classifies resolved IPs
# can be defeated when the host does not resolve in a sandbox yet routes
# to the metadata service in production.
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
    }
)

# RFC 6598 carrier-grade NAT shared address space. Not flagged by the stdlib
# is_private/is_global predicates, but an SSRF guard must refuse it.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

# RFC 3879 deprecated IPv6 site-local space. The stdlib reports it as
# is_global=True / is_private=False (only fe80::/10 link-local is flagged), so
# an SSRF guard built on the stdlib predicates would let it through. Refuse it
# explicitly for the same reason as the CGNAT block.
_SITE_LOCAL_NET = ipaddress.ip_network("fec0::/10")


def _host_to_ip_literal(host: str) -> ipaddress._BaseAddress | None:
    """Canonicalise *host* to an IP address if it denotes one literally.

    Handles dotted IPv4/IPv6, bracket-stripped IPv6, trailing-dot forms,
    and the decimal / hexadecimal integer encodings that defeat a naive
    ``ipaddress.ip_address(hostname)`` guard (e.g. ``2130706433`` and
    ``0x7f000001`` both denote ``127.0.0.1``). Returns ``None`` when the
    host is a DNS name rather than an address literal.
    """
    h = host.strip().rstrip(".")
    if not h:
        return None
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:
        if h.lower().startswith("0x"):
            value = int(h, 16)
        elif h.isdigit():
            value = int(h, 10)
        else:
            return None
        if 0 <= value <= 0xFFFFFFFF:
            return ipaddress.ip_address(value)
    except ValueError:
        pass
    return None


def _ip_is_internal(ip: ipaddress._BaseAddress) -> bool:
    """Return True for any address an SSRF guard must refuse to reach."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # RFC 6598 carrier-grade NAT (100.64.0.0/10) is neither is_private nor
    # is_global per the stdlib, yet it is shared ISP space an SSRF guard must
    # refuse (a request there can hit a sibling tenant behind the CGNAT).
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return True
    # RFC 3879 deprecated IPv6 site-local (fec0::/10): is_global per the stdlib,
    # so the predicates below miss it. Refuse it as the link-local sibling.
    if isinstance(ip, ipaddress.IPv6Address) and ip in _SITE_LOCAL_NET:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _ssrf_block_reason(host: str) -> str | None:
    """Classify *host*; return a reason string if it must be refused.

    Refuses cloud-metadata hostnames, then any literal/encoded address in
    a private, loopback, link-local, reserved, multicast, unspecified, or
    IPv4-mapped-internal range. For DNS names, every resolved address is
    classified -- if ANY resolves internal the destination is refused.
    A name that cannot be resolved is allowed to proceed (the request
    layer will fail to connect; no internal host is reachable).
    """
    if host.rstrip(".").lower() in _METADATA_HOSTNAMES:
        return "cloud-metadata hostname"

    literal = _host_to_ip_literal(host)
    if literal is not None:
        return "internal address" if _ip_is_internal(literal) else None

    try:
        from . import script_executors as _se  # deferred: RULE B seam for socket

        infos = _se.socket.getaddrinfo(host, None)
    except (OSError, ValueError):
        # OSError: name does not resolve. ValueError (covers its UnicodeError
        # subclass and the bare ValueError CPython raises for an embedded NUL
        # byte in the host): the host cannot be resolved or IDNA-encoded (empty
        # label, a label over 63 octets, a surrogate, or a NUL). Either way the
        # host is unreachable, so allowing it is SSRF-safe and the request layer
        # will fail to connect -- but it must fail CLOSED (return None) here, not
        # propagate out of _prepare_http and crash the public execute_script
        # caller, matching the _safe_urlparse fail-closed contract.
        return None
    for info in infos:
        sockaddr = info[4]
        try:
            resolved = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _ip_is_internal(resolved):
            return "resolves to internal address"
    return None


class _SSRFConnectError(OSError):
    """Raised at connect time when a resolved address is internal.

    Subclasses ``OSError`` so the ``requests`` layer treats it as an
    ordinary connection failure (the dispatch is logged ``status=error``,
    never an internal host reached).
    """


def _ssrf_safe_connect(
    address: tuple[str, int],
    timeout: object = None,
    source_address: tuple[str, int] | None = None,
    socket_options: list | None = None,
) -> socket.socket:
    """Resolve *address* ONCE, refuse any internal result, then connect.

    Closes the DNS-rebinding TOCTOU left open by the up-front
    :func:`_ssrf_block_reason` guard: ``requests``/``urllib3`` would
    otherwise re-resolve the hostname independently at connect time, so a
    low-TTL name can answer a public A record to the guard and
    ``169.254.169.254`` to the socket. Here the SAME resolution that is
    validated is the one connected to. Raises :class:`_SSRFConnectError`
    when every resolved address is internal, or the last ``OSError`` when
    no permitted address could be reached.
    """
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    last_err: OSError | None = None
    blocked = False
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            resolved = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _ip_is_internal(resolved):
            blocked = True
            continue
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if socket_options:
                for opt in socket_options:
                    sock.setsockopt(*opt)
            if isinstance(timeout, (int, float)):
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_err = exc
            if sock is not None:
                sock.close()
    if blocked and last_err is None:
        raise _SSRFConnectError(f"blocked internal address for host {host}")
    if last_err is not None:
        raise last_err
    raise _SSRFConnectError(f"could not resolve {host} to a permitted address")


# Lazily-built, process-cached ``requests.Session`` whose HTTPS adapter
# pins each connection to the validated resolution (see _ssrf_safe_connect).
_GUARDED_SESSION = None
_GUARDED_SESSION_LOCK = threading.Lock()

# Per-thread handle to the in-flight dispatch's holder dict. The dispatch worker
# runs the blocking ``requests.post`` on its OWN thread and creates the urllib3
# connection on that same thread, so a recording connection mixin can stash the
# live socket into ``holder["sock"]`` here. On total-deadline ABANDONMENT the
# dispatcher then force-closes that socket (``_abort_dispatch_sock``) so the
# wedged ``post`` unblocks and the worker's ``finally`` releases its
# ``_HTTP_INFLIGHT`` permit PROMPTLY -- otherwise a CONTINUOUS slow-loris
# endpoint (one that dribbles under the per-recv read timeout forever) would pin
# its permit for the whole process lifetime, and 32 such endpoints would starve
# ALL legitimate outbound http for the rest of the install (availability DoS).
_DISPATCH_SOCK = threading.local()


def _record_dispatch_sock(sock: object) -> None:
    """Record the just-connected socket into the active dispatch holder.

    Called from a recording connection's ``connect()`` on the worker thread.
    Records only the FIRST socket of the dispatch (a redirect is disabled, so
    there is normally exactly one) and only when a holder is registered.
    """
    holder = getattr(_DISPATCH_SOCK, "holder", None)
    if holder is not None and sock is not None and "sock" not in holder:
        holder["sock"] = sock


def _abort_dispatch_sock(holder: dict) -> bool:
    """Force-close an abandoned worker's socket so its ``post`` unblocks.

    ``shutdown(SHUT_RDWR)`` (NOT ``close``) is deliberate: shutdown unblocks the
    worker's in-progress read without freeing the fd, so the worker's own urllib3
    cleanup closes it -- avoiding the fd-reuse race that a cross-thread ``close``
    would open. Returns True iff a socket was present to abort.
    """
    sock = holder.get("sock")
    if sock is None:
        return False
    with contextlib.suppress(OSError, AttributeError):
        sock.shutdown(socket.SHUT_RDWR)
    return True


class _SockRecordingMixin:
    """Connection mixin that records its socket after ``connect()`` completes.

    Mixed in BEFORE the urllib3 connection base so ``connect`` resolves here
    first, delegates to the real ``connect`` (which sets ``self.sock`` -- for
    pinned HTTPS that is the TLS-wrapped socket), then records it. Pure stdlib:
    references no urllib3 symbol, so it is safe to define at module import.
    """

    def connect(self):  # type: ignore[override]
        super().connect()
        _record_dispatch_sock(getattr(self, "sock", None))


def _build_guarded_session():
    """Build a ``requests.Session`` that resolve-and-pins every HTTPS conn.

    The custom ``urllib3`` connection overrides ``_new_conn`` to delegate
    to :func:`_ssrf_safe_connect`, so TLS SNI / certificate validation
    still runs against the ORIGINAL hostname while the socket only ever
    connects to a guard-approved address.
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPSConnection
    from urllib3.connectionpool import HTTPSConnectionPool
    from urllib3.poolmanager import PoolManager

    class _PinnedHTTPSConnection(_SockRecordingMixin, HTTPSConnection):
        def _new_conn(self):  # type: ignore[override]
            return _ssrf_safe_connect(
                (self._dns_host, self.port),
                self.timeout,
                source_address=self.source_address,
                socket_options=self.socket_options,
            )

    class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
        ConnectionCls = _PinnedHTTPSConnection

    class _PinnedPoolManager(PoolManager):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.pool_classes_by_scheme = {
                **self.pool_classes_by_scheme,
                "https": _PinnedHTTPSConnectionPool,
            }

    class _SSRFGuardAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            self.poolmanager = _PinnedPoolManager(
                num_pools=connections,
                maxsize=maxsize,
                block=block,
                **pool_kwargs,
            )

    session = requests.Session()
    # trust_env=False is correct for THIS (direct-egress) session only: a
    # configured proxy uses urllib3's ProxyManager, NOT the resolve-and-pin
    # pool above, so honoring a proxy here would silently nullify the DNS pin.
    # Corporate-proxy egress is handled on a SEPARATE path in
    # _dispatch_http_request (which honors the operator's env proxy explicitly);
    # this session is used only when no env proxy applies to the destination.
    session.trust_env = False
    session.mount("https://", _SSRFGuardAdapter())
    return session


def _environ_proxies_for(url: str) -> dict:
    """Return the operator's env-configured proxies for *url*, or ``{}``.

    A non-empty result is the corporate-egress case: the environment mandates
    an outbound proxy for this destination (honoring ``NO_PROXY``), exactly as
    curl / pip / npm / git resolve proxies. Returns ``{}`` (direct egress) when
    no proxy applies or on any resolution error -- fail-closed to the DNS-pinned
    direct path rather than guessing a proxy.
    """
    try:
        import requests

        return requests.utils.get_environ_proxies(url, no_proxy=None) or {}
    except Exception:
        return {}


def _get_guarded_session():
    """Return the process-cached resolve-and-pin session, or None on failure.

    A None return means the custom adapter could not be constructed (e.g.
    an incompatible ``urllib3`` build); callers fall back to the up-front
    guard, which still blocks literal and pre-resolved internal targets.
    """
    # Use _se._GUARDED_SESSION as the canonical state so tests can reset
    # it via ``patch.object(se, "_GUARDED_SESSION", ...)`` (RULE B seam).
    from . import script_executors as _se  # deferred to avoid circular import

    if _se._GUARDED_SESSION is not None:
        return _se._GUARDED_SESSION
    with _GUARDED_SESSION_LOCK:
        if _se._GUARDED_SESSION is None:
            with contextlib.suppress(Exception):
                _se._GUARDED_SESSION = _se._build_guarded_session()
    return _se._GUARDED_SESSION


# Lazily-built capturing ``requests.Session`` used for the two egress paths the
# resolve-and-pin guarded session does NOT cover: the corporate-PROXY path
# (urllib3 routes through a ProxyManager, not the pinned pool) and the direct
# FALLBACK when the guarded session could not be built. It records the live
# socket of each dispatch (see _SockRecordingMixin) so an abandoned slow-loris
# worker can be force-closed at the deadline on these paths too -- WITHOUT the
# DNS pin (the destination was already vetted up-front by _ssrf_block_reason,
# and the proxy hop's rebind defense is delegated to the corporate proxy ACLs).
_CAPTURING_SESSION = None
_CAPTURING_SESSION_LOCK = threading.Lock()


def _build_capturing_session():
    """Build a non-pinning ``requests.Session`` that records each dispatch sock.

    Recording connection classes stash ``self.sock`` after ``connect()`` for
    BOTH schemes and BOTH the direct and proxy (ProxyManager) routings, so the
    dispatcher can force-close an abandoned worker on the proxy / fallback paths.
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    from urllib3.poolmanager import PoolManager

    class _RecHTTPConnection(_SockRecordingMixin, HTTPConnection):
        pass

    class _RecHTTPSConnection(_SockRecordingMixin, HTTPSConnection):
        pass

    class _RecHTTPPool(HTTPConnectionPool):
        ConnectionCls = _RecHTTPConnection

    class _RecHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = _RecHTTPSConnection

    rec_pools = {"http": _RecHTTPPool, "https": _RecHTTPSPool}

    class _CapturingAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            self.poolmanager = PoolManager(
                num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs
            )
            self.poolmanager.pool_classes_by_scheme = dict(rec_pools)

        def proxy_manager_for(self, proxy, **proxy_kwargs):
            if proxy in self.proxy_manager:
                return self.proxy_manager[proxy]
            manager = super().proxy_manager_for(proxy, **proxy_kwargs)
            with contextlib.suppress(Exception):
                manager.pool_classes_by_scheme = dict(rec_pools)
            return manager

    session = requests.Session()
    # Never auto-honor env proxies: the proxy is passed EXPLICITLY per-dispatch
    # in _run, so a stray env var cannot silently re-route a dispatch here.
    session.trust_env = False
    adapter = _CapturingAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _get_capturing_session():
    """Return the process-cached capturing session, or None on build failure."""
    global _CAPTURING_SESSION
    if _CAPTURING_SESSION is not None:
        return _CAPTURING_SESSION
    with _CAPTURING_SESSION_LOCK:
        if _CAPTURING_SESSION is None:
            with contextlib.suppress(Exception):
                _CAPTURING_SESSION = _build_capturing_session()
    return _CAPTURING_SESSION


# Upper bound on simultaneous HTTP dispatch worker threads started for a
# single lifecycle event. Without a cap, an event file with N http entries
# spawns N threads + sockets at once (resource exhaustion).
MAX_HTTP_DISPATCH_THREADS = 32

# Hard cap on the number of http dispatches that may be IN FLIGHT at once,
# counting BOTH live workers and abandoned-but-still-alive ones. The dispatch
# pool above bounds how many workers START concurrently, but a slow-loris
# endpoint makes ``_dispatch_http_request`` ABANDON its inner daemon worker on
# total-deadline expiry (the daemon keeps dribbling, holding one socket/fd). A
# malicious event file with thousands of http entries pointed at such an endpoint
# would otherwise leak O(N) abandoned daemons + fds and exhaust the process fd
# table within a single ``apm install``. This BoundedSemaphore is acquired
# NON-BLOCKING before each worker starts and released only when the worker TRULY
# finishes (its ``finally``), so an abandoned worker keeps holding its permit for
# its real lifetime -- capping live+abandoned http daemons to a constant
# regardless of attacker-chosen N. A dispatch that cannot claim a permit is
# dropped with a logged error (zero added stall); a legitimate install with
# <= MAX_HTTP_DISPATCH_THREADS fast http entries never hits the cap because each
# fast worker releases its permit before the next entry needs it.
_HTTP_INFLIGHT = threading.BoundedSemaphore(MAX_HTTP_DISPATCH_THREADS)

# Hard ceiling on the (attacker-influenced) per-event HTTP timeout. ``timeoutSec``
# in a project apm.yml is otherwise uncapped, so a malicious manifest could set it
# to days; a lifecycle http event is fire-and-forget telemetry, so a small finite
# ceiling is safe and bounds the worst-case hold on the dispatch worker.
_MAX_HTTP_TIMEOUT = 30.0
# Connect-phase timeout, bounded separately from the per-recv read timeout.
_HTTP_CONNECT_TIMEOUT = 10.0
# Grace given to an ABANDONED worker to finish after its socket is force-closed
# at the total deadline. The shutdown() unblocks the wedged read within ms, so
# this only needs to cover thread-scheduling jitter; if the worker somehow does
# not finish in the grace (it will, in practice), its permit is still released
# whenever it eventually does, and the in-flight cap bounds the residual.
_HTTP_ABANDON_GRACE = 1.0


def _coerce_http_deadline(timeout: float) -> float:
    """Clamp the per-event HTTP timeout to a finite, positive ceiling.

    ``ScriptEntry.effective_timeout`` is attacker-influenced (project apm.yml
    ``timeoutSec``) and uncapped; a non-finite or huge value would let a
    misbehaving endpoint hold a dispatch thread effectively forever.
    """
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return _MAX_HTTP_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return _MAX_HTTP_TIMEOUT
    return min(value, _MAX_HTTP_TIMEOUT)


def _connect_layer_host(url: str) -> str | None:
    """Return the host ``requests``/``urllib3`` would actually dial.

    The up-front SSRF guard validates ``urllib.parse.urlparse(url).hostname``,
    but the request layer connects to ``urllib3.util.parse_url(url).host``.
    The two parsers disagree on a hostile authority -- notably a backslash,
    which ``urllib3`` treats as an authority terminator while ``urllib.parse``
    folds it into the host -- so ``https://169.254.169.254\\.evil/`` can pass
    the guard (host looks public/unresolvable) yet dial bare
    ``169.254.169.254``. Returning urllib3's host lets the caller validate the
    SAME value the socket layer uses and reject any mismatch. Returns ``None``
    if urllib3 is unavailable or cannot parse the URL (the caller then falls
    back to the urllib.parse host alone).
    """
    try:
        from urllib3.util import parse_url

        return parse_url(url).host
    except Exception:
        return None


def _normalize_host(host: str | None) -> str:
    """Lower-case and strip IPv6 brackets for a parser-agnostic comparison."""
    if not host:
        return ""
    return host.strip("[]").lower()


def _safe_urlparse(url: str):
    """``urlparse`` that fails closed to ``None`` instead of raising.

    ``urllib.parse.urlparse`` raises ``ValueError`` on a malformed authority
    (e.g. ``https://[::1\\.evil/`` -> "Invalid IPv6 URL"). In the fire path
    that would be an uncaught crash, so callers treat a parse failure as a
    refused (skipped) script -- the same fail-closed posture ``validate`` took
    for this URL class.
    """
    try:
        return urlparse(url)
    except ValueError:
        return None


def _prepare_http(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> tuple[str, str, dict[str, str], float, str, str, str] | None:
    """Validate and build an HTTP dispatch, or return None if refused.

    Security gates (all enforced before any network call): HTTPS-only,
    hostname required, and an SSRF guard that refuses internal / metadata
    destinations (including encoded-IP bypasses). Returns the tuple
    ``(url, payload, headers, timeout, event_name, safe_url, hostname)``.
    """
    url = script.url
    if not url:
        _logger.debug("HTTP script has no URL, skipping")
        return None

    parsed = _safe_urlparse(url)
    if parsed is None:
        if verbose and logger:
            logger.verbose_detail("[i] HTTP script rejected: malformed URL")
        _logger.debug("Rejecting malformed script URL: %s", url)
        return None
    if parsed.scheme != "https":
        if verbose and logger:
            logger.verbose_detail(
                f"[i] HTTP script rejected: URL must use https (got {parsed.scheme}://)"
            )
        _logger.debug("Rejecting non-HTTPS script URL: %s", url)
        return None

    if not parsed.hostname:
        _logger.debug("HTTP script URL has no hostname: %s", url)
        return None

    # Validate the host the CONNECT layer (urllib3) will actually dial, not
    # just urllib.parse's view: a backslash or other authority-confusing
    # character makes them disagree, and the fallback request path would
    # otherwise reach an internal literal the guard was tricked into allowing.
    connect_host = _connect_layer_host(url)
    if connect_host is not None and _normalize_host(connect_host) != _normalize_host(
        parsed.hostname
    ):
        if verbose and logger:
            logger.verbose_detail("[i] HTTP script rejected: ambiguous URL authority")
        _logger.debug(
            "Rejecting URL: guard/connect host mismatch (%r vs %r)",
            parsed.hostname,
            connect_host,
        )
        return None

    # The mismatch gate above guarantees the connect-layer host equals the
    # guard host once we reach here, so a single SSRF resolution on
    # parsed.hostname covers both views -- resolving twice would needlessly
    # widen the DNS-rebind window.
    reason = _ssrf_block_reason(parsed.hostname)
    if reason is not None:
        if verbose and logger:
            logger.verbose_detail(f"[i] HTTP script rejected: {reason}")
        _logger.debug("Rejecting internal/SSRF script URL (%s)", reason)
        return None

    allowed = frozenset(script.allowed_env_vars or ())
    request_headers: dict[str, str] = {"Content-Type": "application/json"}
    if script.headers:
        for key, val in script.headers.items():
            request_headers[key] = _expand_env_vars(val, allowed, logger=logger, verbose=verbose)

    return (
        url,
        _http_payload(event),
        request_headers,
        script.effective_timeout,
        event.event,
        _redact_url_credentials(url),
        parsed.hostname,
    )


def _dispatch_http_request(
    url: str,
    payload: str,
    request_headers: dict[str, str],
    timeout: float,
    event_name: str,
    safe_url: str,
) -> None:
    """Send the prepared POST synchronously and log the outcome.

    ``stream=True`` keeps a malicious endpoint from forcing the whole
    response body into memory: only the status line is consumed.

    requests/urllib3 treat a scalar ``timeout`` as a PER-RECV socket timeout,
    which a slow-loris endpoint resets on every dribbled byte -- so the read of
    the response status/headers could otherwise hang far past the configured
    timeout. We therefore run the request on an inner daemon worker and enforce
    a TOTAL wall-clock deadline: past it, the worker is abandoned (a daemon never
    blocks process exit) and a timeout is logged, so a dribble can never hold the
    dispatch/batch worker -- or the ``apm lifecycle`` join that waits on it.
    """
    total_deadline = _coerce_http_deadline(timeout)
    connect_timeout = min(total_deadline, _HTTP_CONNECT_TIMEOUT)
    holder: dict[str, object] = {}
    from . import (
        script_executors as _se,  # deferred: RULE B seam for _get_guarded/capturing_session
    )

    # Bound live+abandoned http daemons to MAX_HTTP_DISPATCH_THREADS. A
    # non-blocking acquire means a fast endpoint (permit released in _run's
    # finally before the next entry needs it) never false-drops, while a flood
    # of slow-loris entries that abandon their workers cannot leak more than the
    # cap -- the (cap+1)th simply drops, converting unbounded fd-exhaustion into
    # the already-accepted <= cap residual, with zero added stall.
    if not _HTTP_INFLIGHT.acquire(blocking=False):
        _append_to_script_log(
            event_name,
            "http",
            safe_url,
            stderr="too many in-flight http dispatches; entry dropped",
            status="error",
        )
        return

    def _run() -> None:
        _DISPATCH_SOCK.holder = holder
        try:
            import requests

            # The DESTINATION host was already vetted by _ssrf_block_reason in
            # _prepare_http (internal / metadata / loopback targets are refused
            # before dispatch), and that gate is proxy-agnostic -- it runs whether
            # or not a proxy is configured. Egress ROUTING is a separate, operator-
            # owned concern handled here:
            env_proxies = _environ_proxies_for(url)
            if env_proxies:
                # Corporate-egress case: the operator's environment mandates an
                # outbound proxy for this destination (honoring NO_PROXY), exactly
                # as curl / pip / npm / git do -- in many corporate networks the
                # proxy is the ONLY outbound path. The resolve-and-pin adapter
                # cannot apply through a proxy (urllib3 uses a ProxyManager), so
                # rebind-TOCTOU defense for this hop is delegated to the corporate
                # proxy's own egress ACLs; the up-front destination gate still
                # bounds WHERE the request may be sent. Influencing the process
                # environment is RCE-equivalent here anyway (command-type lifecycle
                # scripts run in this same env), so env integrity is a precondition,
                # not a boundary we can meaningfully defend on this path. The
                # capturing session records the proxy socket so an abandoned
                # slow-loris (a dribbling proxy) is force-closed at the deadline.
                capt = _se._get_capturing_session()
                post = capt.post if capt is not None else requests.post
                proxies = env_proxies
            else:
                # Direct-egress case: no env proxy applies. Use the resolve-and-pin
                # guarded session (records its pinned socket) and explicitly refuse
                # any proxy so a stray env var cannot silently nullify the DNS pin.
                # If the guarded session is unavailable, fall back to the capturing
                # session (still records the socket; the up-front gate already
                # blocked internal targets) rather than bare requests.
                session = _se._get_guarded_session()
                if session is None:
                    session = _se._get_capturing_session()
                post = session.post if session is not None else requests.post
                proxies = {"http": None, "https": None}

            holder["resp"] = post(
                url,
                data=payload,
                headers=request_headers,
                timeout=(connect_timeout, total_deadline),
                allow_redirects=False,
                stream=True,
                proxies=proxies,
            )
        except BaseException as exc:
            holder["exc"] = exc
        finally:
            _DISPATCH_SOCK.holder = None
            # Release when the worker finishes. With the deadline force-close
            # below, an abandoned slow-loris worker finishes within the abandon
            # grace (its wedged read is unblocked), so its permit is reclaimed
            # promptly rather than pinned for the process lifetime.
            _HTTP_INFLIGHT.release()

    worker = threading.Thread(target=_run, name="apm-http-post", daemon=True)
    try:
        worker.start()
    except BaseException:
        # The thread never began, so _run's finally will not run; release the
        # permit here to avoid leaking it (e.g. "can't start new thread").
        _HTTP_INFLIGHT.release()
        raise
    worker.join(total_deadline)

    if worker.is_alive():
        # A dribbling endpoint is still feeding bytes under the per-recv read
        # timeout past the total deadline. Force-close the worker's captured
        # socket so its wedged read raises and the worker's finally releases its
        # _HTTP_INFLIGHT permit PROMPTLY (a CONTINUOUS dribble would otherwise
        # pin the permit for the whole process lifetime -> 32 such endpoints
        # starve all legit outbound http). shutdown(SHUT_RDWR) unblocks the read
        # within ms without freeing the fd (no cross-thread fd-reuse race); the
        # worker's own urllib3 cleanup then closes it. A short re-join lets the
        # worker run its finally before we return -- but ONLY when a socket was
        # actually recorded (guarded/capturing path). With no recorded socket
        # (bare-requests fallback) the force-close is a no-op, so the re-join
        # would gain nothing and merely add _HTTP_ABANDON_GRACE of latency per
        # serial dispatch; in that case we abandon at once (the worker is a
        # daemon, reaped at process exit) and reclaim the permit when it dies.
        if _abort_dispatch_sock(holder):
            worker.join(_HTTP_ABANDON_GRACE)
        _append_to_script_log(
            event_name,
            "http",
            safe_url,
            stderr=f"request exceeded total deadline {total_deadline:g}s",
            status="error",
        )
        return

    exc = holder.get("exc")
    if exc is not None:
        _logger.debug("HTTP POST failed for %s", safe_url, exc_info=True)
        _append_to_script_log(event_name, "http", safe_url, stderr=str(exc), status="error")
        return

    resp = holder.get("resp")
    _append_to_script_log(
        event_name,
        "http",
        safe_url,
        stdout=f"HTTP {resp.status_code}",
        status="ok" if resp.ok else "error",
    )
    with contextlib.suppress(Exception):
        resp.close()
