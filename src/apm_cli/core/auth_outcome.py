"""Canonical authentication-outcome classification for remote host access.

Owner of one decision: "did this attempt fail because the presented (or
missing) credential was not accepted, and may the auth chain escalate?".
``AuthResolver.try_with_fallback`` calls :func:`classify_auth_outcome` for
every failed attempt; call sites report raw outcomes (HTTP responses, Git
stderr, exceptions) and never decide auth failure themselves.

Azure DevOps hides one condition behind three response shapes: HTTP 401/403,
a redirect to its sign-in service, and an HTTP 200/203 HTML sign-in page.
All three classify as an auth failure here so every call site escalates the
same way.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from ..utils.credential_rendering import redact_secrets

# Secret redaction (gate for every external diagnostic string) is owned by
# utils/credential_rendering.py.

_MAX_DETAIL_CHARS = 300


class SecretRedactionFilter(logging.Filter):
    """Logging filter that redacts secret patterns from all emitted log records.

    Lives beside :func:`redact_secrets` so the root CLI can install it on every
    run without importing the auth resolver. Install on logging handlers when
    debug logging is enabled so that exception messages carrying HTTP client error strings --
    which some libraries embed auth headers or token values in -- do not leak
    credentials into the debug stream.

    Usage::

        logging.getLogger("apm_cli").addFilter(SecretRedactionFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = redact_secrets(msg)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
            if record.exc_info is not None:
                formatted = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = redact_secrets(formatted)
                record.exc_info = None
        except Exception:
            pass
        return True


def sanitize_diagnostic_detail(text: str | None) -> str:
    """Return the last meaningful line of *text*, redacted and printable ASCII.

    Mandatory gate between external process output (az, git, credential
    helpers) and anything APM records or renders.
    """
    if not text:
        return ""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return ""
    # Prefer the line that states the failure (az prints its AADSTS reason
    # first and remediation commands after it); otherwise use the last line.
    chosen = next(
        (
            line
            for line in lines
            if "aadsts" in line.lower() or line.lower().startswith(("error", "fatal"))
        ),
        lines[-1],
    )
    last = redact_secrets(chosen)
    ascii_only = "".join(ch if 0x20 <= ord(ch) <= 0x7E else "?" for ch in last)
    return ascii_only[:_MAX_DETAIL_CHARS]


# ---------------------------------------------------------------------------
# Outcome model
# ---------------------------------------------------------------------------


class AuthOutcomeKind(StrEnum):
    """Every outcome an authenticated attempt can classify into."""

    OK = "ok"
    AUTH_REQUIRED = "auth_required"
    CREDENTIAL_REJECTED = "credential_rejected"
    NOT_FOUND = "not_found"
    THROTTLED = "throttled"
    NETWORK = "network"
    TLS = "tls"
    OTHER = "other"


_AUTH_FAILURE_KINDS = frozenset(
    {AuthOutcomeKind.AUTH_REQUIRED, AuthOutcomeKind.CREDENTIAL_REJECTED}
)


@dataclass(frozen=True)
class AuthOutcome:
    """Classified result of one attempt; ``detail`` is always redacted.

    ``rejection_verified`` is set only when the server refused the presented
    credential itself: HTTP 401, or Git's wording for a 401. A 403, a sign-in
    redirect, or a sign-in page never sets it, because those also answer a
    valid credential that lacks access.
    """

    kind: AuthOutcomeKind
    detail: str = ""
    status: int | None = None
    rejection_verified: bool = False

    @property
    def is_auth_failure(self) -> bool:
        """Return whether the auth chain may escalate to the next credential."""
        return self.kind in _AUTH_FAILURE_KINDS

    @property
    def is_verified_rejection(self) -> bool:
        """Return whether the presented credential was verifiably refused (HTTP 401)."""
        return self.kind is AuthOutcomeKind.CREDENTIAL_REJECTED and self.rejection_verified


class RepositoryNotFoundError(RuntimeError):
    """The host answered that the repository does not exist or is not visible.

    Raised with a rendered message once every credential the chain may try was
    tried, so callers can name a next step without re-reading Git stderr.
    """


class AuthAttemptFailed(RuntimeError):
    """Report a non-success attempt outcome to the auth chain.

    Operations raise this (instead of a hand-written "unauthorized" error) to
    hand the raw HTTP response or process result to the classifier. Callers
    that need the final raw outcome read ``response`` or ``result`` after the
    chain re-raises it.
    """

    def __init__(
        self,
        message: str = "",
        *,
        response: Any = None,
        result: Any = None,
        stderr: str | None = None,
    ) -> None:
        status = getattr(response, "status_code", None)
        if not message:
            message = f"HTTP {status}" if status is not None else "attempt failed"
        super().__init__(message)
        self.response = response
        self.result = result
        self.stderr = stderr if stderr is not None else getattr(result, "stderr", None)


# ---------------------------------------------------------------------------
# Azure DevOps sign-in recognition
# ---------------------------------------------------------------------------

# Microsoft Learn "Allowed IP addresses and domain URLs" lists these as the
# Azure DevOps "authentication and sign-in" endpoints.
ADO_SIGNIN_HOSTS: frozenset[str] = frozenset(
    {
        "app.vssps.visualstudio.com",
        "app.vssps.dev.azure.com",
        "login.microsoftonline.com",
    }
)
_ADO_SIGNIN_HOST_SUFFIX = ".vssps.visualstudio.com"

# Host kind for hosted ``marketplace.json`` catalogs. A catalog is a JSON
# document, so an HTML page reached through a redirect is a sign-in page.
CATALOG_HOST_KIND = "marketplace-catalog"


def is_ado_signin_location(location: str | None) -> bool:
    """Return whether a redirect target is the Azure DevOps sign-in service."""
    if not location:
        return False
    parsed = urlsplit(location)
    host = (parsed.hostname or "").lower()
    if host in ADO_SIGNIN_HOSTS or host.endswith(_ADO_SIGNIN_HOST_SUFFIX):
        return True
    return "/_signin" in parsed.path.lower()


# ---------------------------------------------------------------------------
# Text signals
# ---------------------------------------------------------------------------

_THROTTLE_PHRASES = ("rate limit", "throttle", "too many requests")
_NETWORK_PHRASES = (
    "could not resolve host",
    "connection refused",
    "connection timed out",
    "network is unreachable",
    "timed out",
)
_TLS_PHRASES = (
    "certificate verify failed",
    "certificate_verify_failed",
    "ssl certificate problem",
    "tls verification failed",
)
# Git, libcurl, and helper wording for "the credential was missing or refused".
_AUTH_PHRASES = (
    "authentication failed",
    "authorization failed",
    "unauthorized",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "unable to get password",
    "invalid username or password",
)
# Word-anchored so Azure DevOps TF401xxx codes never count as HTTP status.
_AUTH_STATUS_RE = re.compile(
    r"(?:http(?: error)?|returned error:|status(?: code)?[=: ]+)\s*(?:401|403)\b|\b(?:401|403)\b",
    re.IGNORECASE,
)
# Git and HTTP wording that only follows an HTTP 401 for a presented credential
# (Git asks for a new credential, or reports the one it sent as failed).
_REJECTED_PHRASES = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "invalid username or password",
)
# Status-anchored only, with a separator: a bare or glued "401" (team-401,
# http401) may be part of a repository URL.
_STATUS_PREFIX = (
    r"(?:\bhttp(?:/\d(?:\.\d)?| error)?\s+|returned error:\s*"
    r"|\bstatus(?: code)?\s*[=:]\s*|\bstatus\s+)"
)
_REJECTED_STATUS_RE = re.compile(_STATUS_PREFIX + r"401\b", re.IGNORECASE)
_FORBIDDEN_STATUS_RE = re.compile(_STATUS_PREFIX + r"403\b", re.IGNORECASE)
# A proxy challenge is about the proxy credential, never the host credential.
_PROXY_SIGNAL_RE = re.compile(r"proxy|\b407\b", re.IGNORECASE)
_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.IGNORECASE)


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _names_other_host(text: str, host: str | None, port: int | None) -> bool:
    """Return whether *text* names a URL other than the requested HTTPS endpoint.

    Without a requested host no URL can be confirmed, so any URL counts.
    """
    if not host:
        return _URL_RE.search(text) is not None
    wanted_port = port if port is not None else _DEFAULT_PORTS["https"]
    for match in _URL_RE.finditer(text):
        try:
            parsed = urlsplit(match.group(0))
            scheme = parsed.scheme.lower()
            other_port = parsed.port if parsed.port is not None else _DEFAULT_PORTS.get(scheme)
        except ValueError:
            return True
        if scheme != "https" or (parsed.hostname or "").lower() != host.lower():
            return True
        if other_port != wanted_port:
            return True
    return False


_PUBLIC_GITHUB_STATUS_RE = re.compile(
    r"(?:http(?: error)?|returned error:|status(?: code)?[=: ]+)\s*(?:401|403|404)\b",
)
# Git's own wording for an HTTP 404 on a repository URL: "fatal: repository
# '<url>' not found". GitHub adds "remote: Repository not found." before it.
_GIT_REPOSITORY_NOT_FOUND_RE = re.compile(r"\brepository '[^'\n]*' not found", re.IGNORECASE)
_NOT_FOUND_RE = re.compile(
    r"\bTF401019\b|repository not found|\b404\b|" + _GIT_REPOSITORY_NOT_FOUND_RE.pattern,
    re.IGNORECASE,
)


def _cause_chain(error: object) -> Iterator[BaseException]:
    """Yield *error* and its causes; tolerate objects that are not exceptions."""
    current = error if isinstance(error, BaseException) else None
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        nxt = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        current = nxt if isinstance(nxt, BaseException) else None


def _normalize_error(error: object) -> BaseException | str | None:
    """Coerce any non-exception, non-text *error* into text for classification."""
    if error is None or isinstance(error, (BaseException, str)):
        return error
    if isinstance(error, bytes):
        return error.decode("utf-8", errors="replace")
    try:
        return str(error)
    except Exception:
        return ""


def _flatten_error(error: BaseException | str | None) -> tuple[str, list[int], list[Any]]:
    """Collect text, HTTP status codes, and responses across the cause chain."""
    if error is None:
        return "", [], []
    if isinstance(error, str):
        return error, [], []
    parts: list[str] = []
    statuses: list[int] = []
    responses: list[Any] = []
    for current in _cause_chain(error):
        response = getattr(current, "response", None)
        if response is not None:
            responses.append(response)
        status = getattr(current, "status_code", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        if isinstance(status, int):
            statuses.append(status)
        parts.append(str(current))
        for name in ("stderr", "stdout"):
            value = getattr(current, name, None)
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if isinstance(value, str) and value:
                parts.append(value)
    return " ".join(parts), statuses, responses


def _is_throttle_error(error: BaseException | str | None) -> bool:
    if error is None or isinstance(error, str):
        return False
    from apm_cli.deps.github_rate_limit import GitHubThrottleError

    return any(isinstance(current, GitHubThrottleError) for current in _cause_chain(error))


def _transport_kind(error: BaseException | str | None) -> AuthOutcomeKind | None:
    if error is None or isinstance(error, str):
        return None
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency
        return None
    for current in _cause_chain(error):
        if isinstance(current, requests.exceptions.SSLError):
            return AuthOutcomeKind.TLS
        if isinstance(current, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return AuthOutcomeKind.NETWORK
    return None


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def _header(headers: Mapping[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower(), "")
    return str(value or "")


def _text_attr(obj: Any, name: str) -> str | None:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) else None


def _failure(kind_presented: bool) -> AuthOutcomeKind:
    return AuthOutcomeKind.CREDENTIAL_REJECTED if kind_presented else AuthOutcomeKind.AUTH_REQUIRED


def _signin_page_outcome(
    response: Any, status: int, *, host_kind: str, credential_presented: bool
) -> AuthOutcome | None:
    """Return the auth failure for a 2xx sign-in page, else ``None``."""
    content_type = _header(getattr(response, "headers", None), "Content-Type").lower()
    if host_kind == "ado" and (status == 203 or "text/html" in content_type):
        return AuthOutcome(
            _failure(credential_presented),
            "Azure DevOps answered with a sign-in page",
            status,
        )
    if (
        host_kind == CATALOG_HOST_KIND
        and "text/html" in content_type
        and getattr(response, "history", None)
    ):
        return AuthOutcome(_failure(credential_presented), "redirected to a sign-in page", status)
    return None


def _classify_response(
    response: Any,
    *,
    host_kind: str,
    credential_presented: bool,
    public_github_anonymous: bool,
) -> AuthOutcome:
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", None)
    is_ado = host_kind == "ado"
    if not isinstance(status, int):
        return AuthOutcome(AuthOutcomeKind.OTHER, "response without status")
    if is_ado:
        # A response that landed on the sign-in service (followed by a
        # redirect helper that keeps no history) is never a direct rejection.
        if is_ado_signin_location(_text_attr(response, "url")):
            return AuthOutcome(
                _failure(credential_presented), "Azure DevOps redirected to sign-in", status
            )
        for previous in getattr(response, "history", None) or ():
            if is_ado_signin_location(_header(getattr(previous, "headers", None), "Location")):
                return AuthOutcome(
                    _failure(credential_presented),
                    "Azure DevOps redirected to sign-in",
                    status,
                )
    if 200 <= status < 300:
        page = _signin_page_outcome(
            response, status, host_kind=host_kind, credential_presented=credential_presented
        )
        return page or AuthOutcome(AuthOutcomeKind.OK, "", status)
    if status in (401, 403):
        return AuthOutcome(
            _failure(credential_presented),
            f"HTTP {status}",
            status,
            rejection_verified=credential_presented and status == 401,
        )
    if 300 <= status < 400:
        location = _header(headers, "Location")
        if is_ado and is_ado_signin_location(location):
            return AuthOutcome(
                _failure(credential_presented),
                "Azure DevOps redirected to sign-in",
                status,
            )
        return AuthOutcome(AuthOutcomeKind.OTHER, f"HTTP {status} redirect", status)
    if status == 404:
        if public_github_anonymous:
            return AuthOutcome(AuthOutcomeKind.AUTH_REQUIRED, "HTTP 404", status)
        return AuthOutcome(AuthOutcomeKind.NOT_FOUND, "HTTP 404", status)
    if status == 429:
        return AuthOutcome(AuthOutcomeKind.THROTTLED, "HTTP 429", status)
    return AuthOutcome(AuthOutcomeKind.OTHER, f"HTTP {status}", status)


def _classify_text(
    text: str,
    statuses: list[int],
    *,
    credential_presented: bool,
    public_github_anonymous: bool,
    host: str | None = None,
    port: int | None = None,
) -> AuthOutcome:
    detail = sanitize_diagnostic_detail(text)
    lowered = text.lower()
    if any(phrase in lowered for phrase in _THROTTLE_PHRASES):
        return AuthOutcome(AuthOutcomeKind.THROTTLED, detail)
    if any(phrase in lowered for phrase in _TLS_PHRASES):
        return AuthOutcome(AuthOutcomeKind.TLS, detail)
    if any(phrase in lowered for phrase in _NETWORK_PHRASES):
        return AuthOutcome(AuthOutcomeKind.NETWORK, detail)
    if public_github_anonymous:
        if any(phrase in lowered for phrase in (*_AUTH_PHRASES, "repository not found")):
            return AuthOutcome(AuthOutcomeKind.AUTH_REQUIRED, detail)
        if _GIT_REPOSITORY_NOT_FOUND_RE.search(text):
            return AuthOutcome(AuthOutcomeKind.AUTH_REQUIRED, detail)
        if any(status in {401, 403, 404} for status in statuses):
            return AuthOutcome(AuthOutcomeKind.AUTH_REQUIRED, detail)
        if _PUBLIC_GITHUB_STATUS_RE.search(lowered):
            return AuthOutcome(AuthOutcomeKind.AUTH_REQUIRED, detail)
        return AuthOutcome(AuthOutcomeKind.OTHER, detail)
    # A 403, not-found, proxy signal, or a URL on another endpoint makes a
    # rejection ambiguous: never verified. URLs are matched separately so a
    # repository named "proxy" or "team-404" does not hide a real rejection.
    prose = _URL_RE.sub(" ", text)
    ambiguous = (
        bool({403, 404} & set(statuses))
        or _FORBIDDEN_STATUS_RE.search(prose) is not None
        or _NOT_FOUND_RE.search(prose) is not None
        or "not found" in prose.lower()
        or _PROXY_SIGNAL_RE.search(prose) is not None
        or _names_other_host(text, host, port)
    )
    if any(status in {401, 403} for status in statuses):
        return AuthOutcome(
            _failure(credential_presented),
            detail,
            rejection_verified=credential_presented and 401 in statuses and not ambiguous,
        )
    if any(phrase in lowered for phrase in _AUTH_PHRASES) or _AUTH_STATUS_RE.search(text):
        verified = (
            credential_presented
            and not ambiguous
            and (
                any(phrase in prose.lower() for phrase in _REJECTED_PHRASES)
                or _REJECTED_STATUS_RE.search(prose) is not None
            )
        )
        return AuthOutcome(_failure(credential_presented), detail, rejection_verified=verified)
    if 404 in statuses or _NOT_FOUND_RE.search(text):
        return AuthOutcome(AuthOutcomeKind.NOT_FOUND, detail)
    return AuthOutcome(AuthOutcomeKind.OTHER, detail)


def classify_auth_outcome(
    *,
    host_info: Any = None,
    host_kind: str | None = None,
    response: Any = None,
    error: object = None,
    credential_presented: bool = False,
    public_github_anonymous: bool = False,
) -> AuthOutcome:
    """Classify one attempt's response or error.

    ``host_info`` (or ``host_kind``) selects the host-class rules. Azure
    DevOps adds sign-in redirect and HTML sign-in page recognition.
    ``public_github_anonymous`` applies only to the anonymous-first
    github.com leg, where a 404 may hide a private repository.
    Connectivity, TLS, and throttle signals always win over auth phrases.
    ``error`` may be any object; non-exception values are read as text.
    """
    error = _normalize_error(error)
    kind_name = host_kind or getattr(host_info, "kind", None)
    if not isinstance(kind_name, str) or not kind_name:
        kind_name = "generic"
    if _is_throttle_error(error):
        return AuthOutcome(AuthOutcomeKind.THROTTLED, sanitize_diagnostic_detail(str(error)))
    transport = _transport_kind(error)
    if transport is not None:
        return AuthOutcome(transport, sanitize_diagnostic_detail(str(error)))
    text, statuses, responses = _flatten_error(error)
    candidate = response if response is not None else (responses[0] if responses else None)
    if candidate is not None and getattr(candidate, "status_code", None) is not None:
        outcome = _classify_response(
            candidate,
            host_kind=kind_name,
            credential_presented=credential_presented,
            public_github_anonymous=public_github_anonymous,
        )
        if outcome.kind is not AuthOutcomeKind.OTHER or not text.strip():
            return outcome
    if error is None:
        return AuthOutcome(AuthOutcomeKind.OK)
    return _classify_text(
        text,
        statuses,
        credential_presented=credential_presented,
        public_github_anonymous=public_github_anonymous,
        host=getattr(host_info, "host", None),
        port=getattr(host_info, "port", None),
    )


def is_repository_not_found(error: object) -> bool:
    """Return whether *error* reports a missing (or invisible) repository.

    True for an HTTP 404, Git's "repository '<url>' not found", or Azure
    DevOps TF401019, with no network, TLS, throttle, or auth signal. The
    public github.com anonymous leg reads the same 404 as "credentials may be
    required"; this answers the question after the chain has run.
    """
    outcome = classify_auth_outcome(error=error, credential_presented=True)
    return outcome.kind is AuthOutcomeKind.NOT_FOUND


def reports_repository_not_found(error: BaseException) -> bool:
    """Return whether *error* or one of its causes is a :class:`RepositoryNotFoundError`."""
    return any(isinstance(current, RepositoryNotFoundError) for current in _cause_chain(error))


def raise_for_auth_outcome(
    response: Any,
    *,
    host_info: Any = None,
    host_kind: str | None = None,
    credential_presented: bool = False,
) -> None:
    """Raise :class:`AuthAttemptFailed` when *response* classifies as an auth failure.

    For operations that consume the response body inside the attempt (for
    example a streamed JSON read) and therefore cannot return the raw
    response to the chain.
    """
    outcome = classify_auth_outcome(
        host_info=host_info,
        host_kind=host_kind,
        response=response,
        credential_presented=credential_presented,
    )
    if outcome.is_auth_failure:
        raise AuthAttemptFailed(outcome.detail, response=response)
