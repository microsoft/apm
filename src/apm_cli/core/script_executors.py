"""Lifecycle script executors -- one per action type.

Each executor isolates failures: it catches all exceptions internally
and logs failures in verbose mode only (using ``[i]`` ASCII symbol).
``http`` scripts dispatch in a background daemon thread; ``command``
scripts run synchronously and can delay the operation up to their timeout.

Two script types (Copilot CLI aligned):

- ``command`` -- shell command via subprocess, event JSON on **stdin**
- ``http``    -- HTTPS POST with JSON body, env-var expansion in headers

Script output is appended to ``~/.apm/logs/scripts.log`` (with known
credential values redacted) so administrators can audit what scripts
produce without enabling verbose CLI output.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger
    from apm_cli.core.lifecycle_scripts import LifecycleEvent, ScriptEntry

_logger = logging.getLogger(__name__)

# Fallback timeouts when script entry does not specify one.
_DEFAULT_HTTP_TIMEOUT = 10
_DEFAULT_COMMAND_TIMEOUT = 30

# Command scripts slower than this (seconds) earn a visible warning, since
# they run synchronously and delay the user-facing operation.
_SLOW_SCRIPT_THRESHOLD_SEC = 5.0

# Pattern for $VAR or ${VAR} expansion in header values.
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# Credential variable denylist -- these must never be expanded into HTTP
# headers or leaked to script subprocesses. The credential token must end
# the name, but we also accept a trailing plural ``S`` and an optional
# ``_ID``/``_IDS`` qualifier so real-world families are caught:
#   - plurals: ...TOKENS, ...KEYS, ...SECRETS, ...CREDENTIALS, ...PATS
#   - qualified: AWS_ACCESS_KEY_ID, ..._KEY_IDS
#   - canonical: GOOGLE_APPLICATION_CREDENTIALS (CREDENTIAL + plural S)
# The trailing ``S?`` does not over-match unrelated names (e.g. PATH keeps
# a stray ``H`` after PAT and so never matches; TRACE_ID has no credential
# token before ``_ID`` and is left alone).
_CREDENTIAL_DENYLIST = re.compile(
    r"(?:TOKEN|SECRET|PAT|KEY|PASSWORD|PASSWD|PWD|CREDENTIAL|AUTHTOKEN)S?(?:_IDS?)?$",
    re.IGNORECASE,
)

# Bare shell variables that end in a denylist token (``PWD``) yet hold no
# secret -- they are the current/previous working directory. Without this
# exemption the ``PWD`` token would sweep the ubiquitous ``$PWD``/``$OLDPWD``
# out of every command env and corrupt logs that echo a path.
_DENYLIST_EXEMPT: frozenset[str] = frozenset({"PWD", "OLDPWD"})

# Credential *blobs* whose NAME ends in a benign suffix (CONFIG / AUTH /
# STRING) that the suffix-token regex cannot express, yet whose VALUE is a
# secret: base64 registry auth (DOCKER_AUTH_CONFIG), a basic-auth header
# (BASIC_AUTH), or a DSN with an embedded password (*_CONNECTION_STRING).
_CREDENTIAL_BLOB_NAMES: frozenset[str] = frozenset({"DOCKER_AUTH_CONFIG", "BASIC_AUTH"})
_CREDENTIAL_BLOB_SUFFIX = re.compile(
    r"(?:_AUTH_CONFIG|_CONNECTION_STRING|CONNECTIONSTRING)$",
    re.IGNORECASE,
)

# Minimum value length that is substring-masked in the audit log. Short
# values (e.g. a 4-char ``test``) are common substrings of ordinary words
# and masking them would corrupt unrelated log text; real credential
# values are long, so an 8-char floor catches secrets without false hits.
_MIN_REDACT_LEN = 8


def _matches_credential(name: str) -> bool:
    """True if *name* conventionally holds a credential value.

    Combines the suffix-token regex with a curated set of credential-blob
    names whose benign suffix the regex cannot express. Bare shell
    working-directory vars (``PWD`` / ``OLDPWD``) are exempt.
    """
    upper = name.upper()
    if upper in _DENYLIST_EXEMPT:
        return False
    if _CREDENTIAL_DENYLIST.search(name):
        return True
    if upper in _CREDENTIAL_BLOB_NAMES:
        return True
    return bool(_CREDENTIAL_BLOB_SUFFIX.search(name))


# Known APM auth variables that must NEVER be expanded even when listed in
# allowedEnvVars -- these are the credentials APM itself uses and must not
# leak to HTTP endpoints or subprocess stdin regardless of opt-in.
_NEVER_EXPAND: frozenset[str] = frozenset(
    {
        "GITHUB_APM_PAT",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ADO_APM_PAT",
    }
)


def _is_denylisted(name: str, allowed: frozenset[str]) -> bool:
    """True if *name* is a credential var NOT explicitly allowlisted.

    _NEVER_EXPAND vars are always blocked regardless of allowedEnvVars.
    """
    if name in _NEVER_EXPAND:
        return True
    if name in allowed:
        return False
    return _matches_credential(name)


def _redact_secrets(text: str) -> str:
    """Mask any denylisted env-var *values* appearing in script output.

    Scripts frequently echo their environment; without this, a command
    that prints ``$ANALYTICS_TOKEN`` would persist the cleartext secret
    into ``~/.apm/logs/scripts.log``. We replace raw occurrences of every
    denylisted variable's value with ``[REDACTED]``.

    Replacement is a raw substring match (not boundary-aware): the value
    is a KNOWN secret, so it must be masked even when glued to adjacent
    word characters. A length floor (``_MIN_REDACT_LEN``) keeps short,
    common values from corrupting unrelated text.

    Values are redacted LONGEST-FIRST so a shorter credential that is a
    prefix/substring of a longer one cannot fragment the longer value and
    leak its tail (e.g. token ``abcd1234`` and ``abcd1234efgh5678`` -- if
    the short one ran first it would split the long one, leaving
    ``efgh5678`` in cleartext).
    """
    if not text:
        return text
    secrets = [
        value
        for name, value in os.environ.items()
        if value and len(value) >= _MIN_REDACT_LEN and _matches_credential(name)
    ]
    redacted = text
    for value in sorted(set(secrets), key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _redact_url_credentials(url: str) -> str:
    """Strip ``user:password@`` from a URL before logging."""
    try:
        parsed = urlparse(url)
        if not parsed.netloc or "@" not in parsed.netloc:
            return url
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return parsed._replace(netloc=host).geturl()
    except (ValueError, TypeError):
        return url


# -- Script output log -----------------------------------------------------

# Per-entry stdout/stderr is truncated to this many characters before being
# written, so a single lifecycle command that prints a large blob cannot
# bloat the audit log (or be used for a local disk-fill DoS).
_MAX_LOG_FIELD_CHARS = 4096

# When the log grows past this size it is rotated to ``scripts.log.1`` so it
# never grows without bound across many noisy events.
_MAX_LOG_BYTES = 5 * 1024 * 1024


def _get_scripts_log_path() -> Path:
    """Return the path to the scripts output log file."""
    apm_home = os.environ.get("APM_HOME")
    base = Path(apm_home) if apm_home else Path.home() / ".apm"
    return base / "logs" / "scripts.log"


_LINE_BREAK_ESCAPES = {
    "\r": "\\r",
    "\n": "\\n",
}
# Every code point str.splitlines() treats as a line boundary. Escaping only
# CR/LF is insufficient: a splitlines()-based log consumer (a very common
# Python idiom) also splits on VT, FF, the FS/GS/RS information separators,
# NEL, LS, and PS -- so an attacker field carrying any of these would still
# forge a column-0 audit record for such a parser. We neutralize the full set
# in one choke point so stdout/stderr (via _truncate_log_field) and target
# (via safe_target) are all covered.
_LINE_BREAK_PATTERN = re.compile("[\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def _neutralize_newlines(text: str) -> str:
    """Escape every line-boundary code point so a log field cannot forge a line.

    The scripts.log audit trail exists to catch malicious scripts, so a field
    derived from attacker-controlled stdout/stderr (or a multi-line command)
    must never contain a raw line break -- otherwise a script could emit output
    that, at column 0, is byte-indistinguishable from a genuine
    ``[ts] event=... status=ok`` entry and forge or bury audit records. The
    header path already strips CR/LF from expanded values; this closes the same
    gap for the output fields, covering the complete ``str.splitlines()``
    boundary set (not just CR/LF) so splitlines-based consumers cannot be
    fooled either.
    """

    def _escape(match: re.Match[str]) -> str:
        char = match.group()
        readable = _LINE_BREAK_ESCAPES.get(char)
        if readable is not None:
            return readable
        code = ord(char)
        return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"

    return _LINE_BREAK_PATTERN.sub(_escape, text)


def _truncate_log_field(text: str) -> str:
    """Clamp a stdout/stderr field to a bounded length for the log.

    Newlines are neutralized first so a single log event occupies a bounded,
    single logical region no attacker field can break out of.
    """
    text = _neutralize_newlines(text)
    if len(text) <= _MAX_LOG_FIELD_CHARS:
        return text
    return text[:_MAX_LOG_FIELD_CHARS] + " ...[truncated]"


def _rotate_log_if_large(log_path: Path) -> None:
    """Rotate the log to ``.1`` once it exceeds the size cap."""
    with contextlib.suppress(OSError):
        if log_path.stat().st_size >= _MAX_LOG_BYTES:
            rotated = log_path.with_name(log_path.name + ".1")
            os.replace(log_path, rotated)


def _append_to_script_log(
    event_name: str,
    script_type: str,
    target: str,
    *,
    stdout: str = "",
    stderr: str = "",
    status: str = "ok",
    exit_code: int | None = None,
) -> None:
    """Append a timestamped entry to the scripts log file.

    Creates ``~/.apm/logs/`` (mode ``0700``) on first write and opens the
    log ``0600`` with ``O_NOFOLLOW`` so it cannot be world-readable nor
    redirected through a pre-planted symlink. Per-entry output is truncated
    and the file is size-rotated. Errors are silently swallowed -- logging
    must never break the CLI.
    """
    try:
        log_path = _get_scripts_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _rotate_log_if_large(log_path)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        safe_target = _neutralize_newlines(_redact_secrets(target))
        lines = [
            f"[{ts}] event={event_name} type={script_type} target={safe_target} status={status}"
        ]
        if exit_code is not None:
            lines[0] += f" exit_code={exit_code}"
        if stdout and stdout.strip():
            lines.append(f"  stdout: {_truncate_log_field(_redact_secrets(stdout.strip()))}")
        if stderr and stderr.strip():
            lines.append(f"  stderr: {_truncate_log_field(_redact_secrets(stderr.strip()))}")
        lines.append("")  # blank line separator

        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(log_path, flags, 0o600)
        try:
            os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        _logger.debug("Failed to write to scripts log", exc_info=True)


def execute_script(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> threading.Thread | None:
    """Dispatch to the correct executor based on script type.

    Returns the daemon thread for HTTP scripts (so callers can optionally
    join it), or None for command scripts and no-ops.
    """
    if script.script_type == "http":
        return _execute_http(script, event, logger=logger, verbose=verbose)
    elif script.script_type == "command":
        _execute_command(script, event, logger=logger, verbose=verbose, project_root=project_root)
    return None


# -- HTTP executor ---------------------------------------------------------


def _expand_env_vars(
    value: str,
    allowed: frozenset[str] = frozenset(),
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> str:
    """Expand ``$VAR`` and ``${VAR}`` references in *value*.

    Variables whose names match the credential denylist pattern
    (TOKEN, SECRET, PAT, KEY, PASSWORD, CREDENTIAL, AUTHTOKEN) are NOT
    expanded unless their name is in *allowed* (the script's opt-in
    ``allowedEnvVars``). A blocked expansion emits a visible warning so
    the failure is never silent.
    """

    def _replace(match: re.Match) -> str:
        var_name = match.group(1) or match.group(2)
        if _is_denylisted(var_name, allowed):
            warning = (
                f"[!] Script: credential variable '{var_name}' will NOT be expanded. "
                f"If you must pass it, add it to the script's 'allowedEnvVars' -- "
                f"note this sends its value to the configured endpoint or subprocess."
            )
            if logger is not None:
                warn_fn = getattr(logger, "warning", None) or getattr(
                    logger, "verbose_detail", None
                )
                if warn_fn is not None:
                    warn_fn(warning)
            _logger.debug("Blocked credential variable expansion: %s", var_name)
            return ""
        # Strip CR/LF so a value carrying a smuggled "\r\nX-Evil: ..." cannot
        # inject extra HTTP headers when expanded into a header value.
        return os.environ.get(var_name, "").replace("\r", "").replace("\n", "")

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _http_payload(event: LifecycleEvent) -> str:
    """Serialise *event* for HTTP delivery with PII minimisation.

    The full ``working_directory`` absolute path leaks the developer's
    username and local filesystem layout to a remote endpoint. For HTTP
    scripts we send only the final path component (the project folder
    name); command scripts -- which run locally -- still receive the full
    path on stdin.
    """
    from dataclasses import replace

    wd = event.working_directory
    safe_wd = Path(wd).name if wd else ""
    return replace(event, working_directory=safe_wd).to_json()


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
        infos = socket.getaddrinfo(host, None)
    except OSError:
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

    class _PinnedHTTPSConnection(HTTPSConnection):
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
    session.mount("https://", _SSRFGuardAdapter())
    return session


def _get_guarded_session():
    """Return the process-cached resolve-and-pin session, or None on failure.

    A None return means the custom adapter could not be constructed (e.g.
    an incompatible ``urllib3`` build); callers fall back to the up-front
    guard, which still blocks literal and pre-resolved internal targets.
    """
    global _GUARDED_SESSION
    if _GUARDED_SESSION is not None:
        return _GUARDED_SESSION
    with _GUARDED_SESSION_LOCK:
        if _GUARDED_SESSION is None:
            with contextlib.suppress(Exception):
                _GUARDED_SESSION = _build_guarded_session()
    return _GUARDED_SESSION


# Upper bound on simultaneous HTTP dispatch worker threads started for a
# single lifecycle event. Without a cap, an event file with N http entries
# spawns N threads + sockets at once (resource exhaustion).
MAX_HTTP_DISPATCH_THREADS = 32


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

    parsed = urlparse(url)
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
    """
    try:
        import requests

        session = _get_guarded_session()
        post = session.post if session is not None else requests.post
        resp = post(
            url,
            data=payload,
            headers=request_headers,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        _append_to_script_log(
            event_name,
            "http",
            safe_url,
            stdout=f"HTTP {resp.status_code}",
            status="ok" if resp.ok else "error",
        )
    except Exception as exc:
        _logger.debug("HTTP POST failed for %s", safe_url, exc_info=True)
        _append_to_script_log(event_name, "http", safe_url, stderr=str(exc), status="error")


def _execute_http(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> threading.Thread | None:
    """Send an HTTP POST to the script URL in a daemon thread.

    Returns the started thread so callers can optionally join it, or
    ``None`` when the destination is refused by a security gate.

    Security hardening:
    - HTTPS-only (rejects ``http://``)
    - SSRF guard (refuses internal / loopback / link-local / metadata)
    - No redirect following
    - ``stream=True`` (response body never buffered)
    - Configurable timeout (default 10s)
    - Header values support ``$ENV_VAR`` expansion (credential vars blocked)
    """
    prepared = _prepare_http(script, event, logger=logger, verbose=verbose)
    if prepared is None:
        return None

    url, payload, request_headers, timeout, event_name, safe_url, hostname = prepared

    thread = threading.Thread(
        target=_dispatch_http_request,
        args=(url, payload, request_headers, timeout, event_name, safe_url),
        daemon=True,
    )
    thread.start()

    if verbose and logger:
        logger.verbose_detail(f"[i] {event.event} event dispatched to {hostname}")

    return thread


def dispatch_http_batch(
    scripts: list[ScriptEntry],
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> list[threading.Thread]:
    """Dispatch many HTTP scripts through a bounded worker pool.

    Starts at most ``MAX_HTTP_DISPATCH_THREADS`` worker threads that drain
    a shared queue, so an event with hundreds of http entries can never
    spawn hundreds of simultaneous threads/sockets. Returns the started
    worker threads so callers can join them. Per-entry SSRF/HTTPS gating
    is applied inside each worker via :func:`_prepare_http`.
    """
    import queue

    if not scripts:
        return []

    work: queue.Queue[ScriptEntry] = queue.Queue()
    for script in scripts:
        work.put(script)

    def _worker() -> None:
        while True:
            try:
                script = work.get_nowait()
            except queue.Empty:
                return
            try:
                prepared = _prepare_http(script, event, logger=logger, verbose=verbose)
                if prepared is not None:
                    url, payload, headers, timeout, event_name, safe_url, _host = prepared
                    _dispatch_http_request(url, payload, headers, timeout, event_name, safe_url)
            except Exception:
                _logger.debug("HTTP dispatch worker failed", exc_info=True)
            finally:
                work.task_done()

    pool_size = min(len(scripts), MAX_HTTP_DISPATCH_THREADS)
    workers = [threading.Thread(target=_worker, daemon=True) for _ in range(pool_size)]
    for worker in workers:
        worker.start()
    return workers


# -- Command executor ------------------------------------------------------


def _execute_command(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> None:
    """Execute a shell command with the event payload on stdin.

    Command scripts run synchronously (bounded by ``timeout``), unlike
    HTTP scripts which fire in a background thread.  The timeout default
    is 30s per script -- multiple slow scripts can accumulate, but each
    is capped.
    """
    cmd = script.effective_command
    if not cmd:
        _logger.debug("Command script has no command string, skipping")
        return

    env = _build_script_env(script)
    timeout = script.effective_timeout
    cwd = _resolve_cwd(script, project_root)

    start = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    try:
        # start_new_session puts the shell (and every grandchild it
        # spawns) in its OWN process group, so a timeout can reap the
        # whole tree via killpg instead of orphaning backgrounded
        # grandchildren (subprocess.run's timeout kills only the shell).
        proc = subprocess.Popen(
            cmd,
            shell=True,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(input=event.to_json(), timeout=timeout)
        returncode = proc.returncode
        _append_to_script_log(
            event.event,
            "command",
            cmd,
            stdout=stdout,
            stderr=stderr,
            exit_code=returncode,
            status="ok" if returncode == 0 else "error",
        )
        elapsed = time.monotonic() - start
        if elapsed > _SLOW_SCRIPT_THRESHOLD_SEC and logger is not None:
            warn = getattr(logger, "warning", None) or getattr(logger, "verbose_detail", None)
            if warn is not None:
                warn(
                    f"[!] Lifecycle command script took {elapsed:.1f}s "
                    "(command scripts run synchronously and delay the operation)."
                )
    except subprocess.TimeoutExpired:
        _logger.debug("Command script timed out: %s", cmd)
        _kill_process_group(proc)
        _append_to_script_log(event.event, "command", cmd, status="timeout")
        if logger:
            warn = getattr(logger, "warning", None) or getattr(logger, "verbose_detail", None)
            if warn is not None:
                warn(
                    f"[!] Lifecycle command script timed out after {script.effective_timeout}s: {cmd}"
                )
    except Exception as exc:
        _logger.debug("Command script failed: %s", cmd, exc_info=True)
        _kill_process_group(proc)
        _append_to_script_log(event.event, "command", cmd, stderr=str(exc), status="error")
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle command script failed: {cmd}")


def _kill_process_group(proc: subprocess.Popen | None) -> None:
    """Kill the script's whole process group, then reap it.

    Required after a timeout: shell=True + start_new_session means the
    shell leads its own process group, so a single SIGKILL to the group
    also reaps backgrounded grandchildren that would otherwise orphan
    (the shell may have already exited while a grandchild lives on).
    Best-effort -- never raises into the install flow.
    """
    if proc is None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, ValueError, OSError):
        proc.communicate(timeout=5)


# -- Helpers ---------------------------------------------------------------


def _build_script_env(script: ScriptEntry) -> dict[str, str]:
    """Build the environment dict for command scripts.

    Inherits the current process environment but strips any variables
    whose names match the credential denylist (TOKEN, SECRET, PAT, KEY,
    PASSWORD, CREDENTIAL, AUTHTOKEN) to prevent accidental exfiltration
    via scripts. A script may opt specific names back in via
    ``allowedEnvVars`` (e.g. ``ANALYTICS_TOKEN``) -- this is best-effort
    convenience, NOT a security boundary: a command script can read any
    file it has permission to.
    """
    allowed = frozenset(script.allowed_env_vars or ())
    env = {k: v for k, v in os.environ.items() if not _is_denylisted(k, allowed)}
    if script.env:
        # script.env values are merged last and may reintroduce credential-named
        # variables deliberately set by the script author. This is intentional
        # best-effort convenience (the user configured it explicitly), NOT a
        # security boundary: a command script can read any file it has permission
        # to regardless of env filtering.
        env.update(script.env)
    return env


def _resolve_cwd(script: ScriptEntry, project_root: str | None) -> str | None:
    """Resolve the working directory for a command script.

    Rejects relative paths that escape project_root to prevent lateral
    movement (e.g. 'cwd: ../../.ssh').  Absolute cwd values are passed
    through unchanged because they are explicit and visible in apm.yml.
    """
    if not script.cwd:
        return project_root
    from pathlib import Path

    if Path(script.cwd).is_absolute():
        return script.cwd
    root = Path(project_root) if project_root else Path.cwd()
    resolved = (root / script.cwd).resolve()
    root_resolved = root.resolve()
    if not str(resolved).startswith(str(root_resolved) + "/") and resolved != root_resolved:
        _logger.warning(
            "Script cwd '%s' escapes project root -- using project root instead", script.cwd
        )
        return str(root_resolved)
    return str(resolved)
