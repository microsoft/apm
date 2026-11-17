"""Azure DevOps authentication chain executed by ``AuthResolver.try_with_fallback``.

The chain table below is the single statement of credential order per Azure
DevOps host class. :func:`run_ado_chain` walks it, asks
:mod:`apm_cli.core.auth_outcome` to classify every failure, and records each
step in an :class:`AuthAttemptLog` that diagnostics render verbatim.

Invariants:

* A step runs only after the previous step's outcome classified as an auth
  failure. Network, TLS, throttle, and not-found outcomes surface unchanged.
* The helper step asks ``git credential fill`` only for the host and
  repository path the caller derived from the dependency reference, caches the
  answer per (host, port, path), and presents it as ``Basic
  username:password`` -- the same header Git itself would send.
* APM never stores or erases a helper credential (no approve or reject call).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from apm_cli.core.auth_outcome import (
    AuthAttemptFailed,
    AuthOutcomeKind,
    classify_auth_outcome,
    sanitize_diagnostic_detail,
)

if TYPE_CHECKING:
    from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo
    from apm_cli.core.token_manager import GitHelperCredential

T = TypeVar("T")

logger = logging.getLogger(__name__)

STEP_PAT = "ADO_APM_PAT"
STEP_BEARER = "az CLI bearer"
STEP_HELPER = "git credential fill"
STEP_ANONYMOUS = "no credential"

# Attempt statuses beyond AuthOutcomeKind for steps that never sent a request.
STATUS_UNAVAILABLE = "unavailable"
STATUS_NO_CREDENTIAL = "no_credential"

# Credential order per Azure DevOps host class (Atlas-confirmed precedence).
ADO_SERVICES_CHAIN: tuple[str, ...] = (STEP_PAT, STEP_BEARER, STEP_HELPER)
ADO_SERVER_CHAIN: tuple[str, ...] = (STEP_PAT, STEP_HELPER)


def ado_chain_steps(bearer_supported: bool) -> tuple[str, ...]:
    """Return the ordered credential steps for one Azure DevOps host class."""
    return ADO_SERVICES_CHAIN if bearer_supported else ADO_SERVER_CHAIN


class AdoAuthChainExhaustedError(RuntimeError):
    """ADO PAT/az/fill chain ended without a usable credential."""

    def __init__(self, message: str, *, attempts: AuthAttemptLog | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True)
class AuthAttempt:
    """One recorded chain step; ``detail`` is redacted printable ASCII."""

    step: str
    status: str
    detail: str = ""

    def describe(self) -> str:
        """Render this step as one diagnostic clause."""
        detail = f" ({self.detail})" if self.detail else ""
        if self.status == STATUS_UNAVAILABLE:
            return f"{self.step} could not get a token{detail}"
        if self.status == STATUS_NO_CREDENTIAL:
            return f"{self.step} returned no credential{detail}"
        if self.step == STEP_ANONYMOUS:
            return f"Without a credential, {self.detail or 'the request was not accepted'}"
        if self.status in (AuthOutcomeKind.AUTH_REQUIRED, AuthOutcomeKind.CREDENTIAL_REJECTED):
            return f"{self.step} was rejected{detail}"
        if self.status == AuthOutcomeKind.OK:
            return f"{self.step} succeeded"
        return f"{self.step} failed: {self.status}{detail}"


@dataclass
class AuthAttemptLog:
    """Ordered, thread-safe record of what the auth chain tried."""

    entries: list[AuthAttempt] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, step: str, status: str, detail: str = "") -> AuthAttempt:
        """Append one step; *detail* is sanitized before it is stored."""
        attempt = AuthAttempt(step, str(status), sanitize_diagnostic_detail(detail))
        with self._lock:
            self.entries.append(attempt)
        return attempt

    def snapshot(self) -> tuple[AuthAttempt, ...]:
        with self._lock:
            return tuple(self.entries)

    def has(self, step: str, status: str) -> bool:
        """Return whether *step* was recorded with *status*."""
        return any(a.step == step and a.status == status for a in self.snapshot())

    def tried_steps(self) -> list[str]:
        """Return the credential steps that ran, in order, without duplicates."""
        names: list[str] = []
        for attempt in self.snapshot():
            if attempt.step != STEP_ANONYMOUS and attempt.step not in names:
                names.append(attempt.step)
        return names

    def render(self) -> str:
        """Render every recorded step as one '; '-joined sentence body."""
        return "; ".join(attempt.describe() for attempt in self.snapshot())

    def render_chain(self, steps: tuple[str, ...]) -> str:
        """Render the full chain in order, naming steps that could not run."""
        recorded = self.snapshot()
        clauses: list[str] = []
        for step in steps:
            matches = [attempt for attempt in recorded if attempt.step == step]
            if matches:
                clauses.append(matches[-1].describe())
            elif step == STEP_PAT:
                clauses.append("ADO_APM_PAT is not set")
            elif step == STEP_BEARER:
                clauses.append("az CLI bearer was not available")
            else:
                clauses.append(f"{step} was not tried")
        return "; ".join(clauses)


def ado_helper_path(path: str | None, host: str | None = None) -> str | None:
    """Return the repository path Git sends for an Azure DevOps remote.

    ``org/project/repo`` becomes ``org/project/_git/repo`` so a helper with
    ``credential.useHttpPath`` (GCM sets it for dev.azure.com) sees the same
    path it stored when Git cloned that repository. Legacy
    ``<org>.visualstudio.com`` hosts carry the organization in the hostname,
    so their Git path is ``project/_git/repo``.
    """
    if not path:
        return None
    segments = [segment for segment in path.strip("/").split("/") if segment]
    host_name = (host or "").lower()
    legacy_org = (
        host_name[: -len(".visualstudio.com")] if host_name.endswith(".visualstudio.com") else ""
    )
    if legacy_org and len(segments) >= 3 and segments[0].lower() == legacy_org:
        segments = segments[1:]
    if "_git" not in segments:
        if len(segments) == 3:
            org, project, repo = segments
            segments = [org, project, "_git", repo]
        elif legacy_org and len(segments) == 2:
            project, repo = segments
            segments = [project, "_git", repo]
    return "/".join(segments) or None


def authorization_headers(git_env: dict[str, str] | None) -> dict[str, str]:
    """Return the HTTP ``Authorization`` header carried by an attempt env.

    ``AuthResolver._build_git_env`` is the only formatter of attempt
    credentials; HTTP consumers reuse its exact header so Git and HTTP present
    the same credential. Returns an empty dict for a credential-free attempt.
    """
    env = git_env or {}
    try:
        count = max(0, int(env.get("GIT_CONFIG_COUNT", "0") or "0"))
    except (TypeError, ValueError):
        count = 0
    header = ""
    for index in range(count):
        if str(env.get(f"GIT_CONFIG_KEY_{index}", "")).lower() != "http.extraheader":
            continue
        header = str(env.get(f"GIT_CONFIG_VALUE_{index}", ""))
    name, separator, value = header.partition(":")
    if not separator or name.strip().lower() != "authorization" or not value.strip():
        return {}
    return {"Authorization": value.strip()}


class AttemptAuth:
    """Requests ``auth=`` hook that applies exactly the chain-selected header.

    Passing an explicit ``auth`` makes Requests skip ambient netrc lookup, so
    a session shared with other consumers still sends only the credential
    the auth chain chose (or none for an anonymous attempt).
    """

    def __init__(self, git_env: dict[str, str] | None) -> None:
        self._headers = authorization_headers(git_env)

    def __call__(self, request: Any) -> Any:
        request.headers.pop("Authorization", None)
        request.headers.update(self._headers)
        return request

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AttemptAuth) and other._headers == self._headers

    def __hash__(self) -> int:
        return hash(tuple(sorted(self._headers.items())))


class HelperCredentialCache:
    """Per-resolver singleflight cache of path-scoped helper answers."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, int | None, str | None], GitHelperCredential | None] = {}
        self._lock = threading.Lock()

    def get(
        self,
        key: tuple[str, int | None, str | None],
        fill: Callable[[], GitHelperCredential | None],
    ) -> GitHelperCredential | None:
        """Return the cached answer, running *fill* at most once per key."""
        with self._lock:
            if key in self._values:
                return self._values[key]
            value = fill()
            self._values[key] = value
            return value

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


def _bearer_provider(resolver: AuthResolver, host: str):
    if not resolver._supports_ado_bearer(host):
        return None
    from apm_cli.core import azure_cli

    return azure_cli.get_bearer_provider()


def _record_resolve_time_bearer_failure(
    resolver: AuthResolver, host: str, attempts: AuthAttemptLog
) -> None:
    provider = _bearer_provider(resolver, host)
    if provider is None or not provider.is_available():
        return
    error = getattr(provider, "last_error", None)
    if isinstance(error, BaseException):
        attempts.record(
            STEP_BEARER, STATUS_UNAVAILABLE, getattr(error, "stderr", None) or str(error)
        )


def _exhausted(
    resolver: AuthResolver,
    host_info: HostInfo,
    attempts: AuthAttemptLog,
    cause: BaseException,
) -> AdoAuthChainExhaustedError:
    if resolver._supports_ado_bearer(host_info.host):
        recovery = (
            "Refresh ADO_APM_PAT, run 'az login', or store "
            "a repository credential in Git Credential Manager, then retry."
        )
    else:
        recovery = (
            "Refresh ADO_APM_PAT or store a repository credential "
            "in Git Credential Manager, then retry."
        )
    anonymous = [a for a in attempts.snapshot() if a.step == STEP_ANONYMOUS]
    observed = f" {anonymous[-1].describe()}." if anonymous else ""
    steps = ado_chain_steps(resolver._supports_ado_bearer(host_info.host))
    error = AdoAuthChainExhaustedError(
        f"Azure DevOps authentication failed. Tried in order: "
        f"{attempts.render_chain(steps)}.{observed} {recovery}",
        attempts=attempts,
    )
    error.__cause__ = cause
    return error


def run_ado_chain(
    resolver: AuthResolver,
    host_info: HostInfo,
    operation: Callable[..., T],
    *,
    ctx: AuthContext,
    path: str | None,
    env_for: Callable[[AuthContext], dict[str, str]],
    log: Callable[[str], None],
    attempts: AuthAttemptLog,
) -> T:
    """Walk ``ADO_APM_PAT -> az bearer -> path-scoped fill`` for one operation."""
    from apm_cli.core.auth import AuthContext
    from apm_cli.core.token_manager import GitHubTokenManager

    display = host_info.display_name
    if ctx.source == STEP_PAT:
        first_step = STEP_PAT
    elif ctx.source == GitHubTokenManager.ADO_BEARER_SOURCE:
        first_step = STEP_BEARER
    else:
        first_step = STEP_ANONYMOUS
        _record_resolve_time_bearer_failure(resolver, host_info.host, attempts)

    def _attempt(step: str, attempt_ctx: AuthContext) -> tuple[bool, Any]:
        try:
            value = operation(attempt_ctx.token, env_for(attempt_ctx))
            if isinstance(getattr(value, "status_code", None), int):
                # A returned HTTP response is an outcome too: Azure DevOps
                # answers a missing credential with 200/203 + a sign-in page.
                returned = classify_auth_outcome(
                    host_info=host_info,
                    response=value,
                    credential_presented=bool(attempt_ctx.token),
                )
                if returned.is_auth_failure:
                    raise AuthAttemptFailed(returned.detail, response=value)
            return True, value
        except Exception as exc:
            # operation is caller-provided; the classifier decides what happens next.
            outcome = classify_auth_outcome(
                host_info=host_info,
                error=exc,
                credential_presented=bool(attempt_ctx.token),
            )
            entry = attempts.record(step, outcome.kind, outcome.detail)
            log(f"{display}: {entry.describe()}")
            if not outcome.is_auth_failure:
                raise
            return False, exc

    log(f"Auth-only attempt for ado host {display} ({first_step})")
    ok, value = _attempt(first_step, ctx)
    if ok:
        return value
    last_exc: BaseException = value

    if first_step == STEP_PAT and STEP_BEARER in ado_chain_steps(
        resolver._supports_ado_bearer(host_info.host)
    ):
        provider = _bearer_provider(resolver, host_info.host)
        if provider is not None and provider.is_available():
            try:
                bearer = provider.get_bearer_token()
            except Exception as bearer_error:
                # Any acquisition failure (AzureCliBearerError or an unexpected
                # provider error) means no bearer; the chain moves on.
                entry = attempts.record(
                    STEP_BEARER,
                    STATUS_UNAVAILABLE,
                    getattr(bearer_error, "stderr", None) or str(bearer_error),
                )
                log(f"{display}: {entry.describe()}")
            else:
                bearer_ctx = AuthContext(
                    token=bearer,
                    source=GitHubTokenManager.ADO_BEARER_SOURCE,
                    token_type="unknown",  # noqa: S106 - token kind label, not a secret
                    host_info=host_info,
                    git_env={},
                    auth_scheme="bearer",
                )
                ok, value = _attempt(STEP_BEARER, bearer_ctx)
                if ok:
                    resolver.emit_stale_pat_diagnostic(display)
                    return value
                last_exc = value

    if not resolver._allow_external_fallback:
        raise last_exc
    helper_path = ado_helper_path(path, host_info.host)
    path_suffix = f" (path={helper_path})" if helper_path else ""
    log(f"trying git credential fill for {display}{path_suffix}")
    credential = resolver._helper_credential(host_info, helper_path)
    if credential is None:
        entry = attempts.record(
            STEP_HELPER,
            STATUS_NO_CREDENTIAL,
            f"for {display}/{helper_path}" if helper_path else f"for {display}",
        )
        log(f"{display}: {entry.describe()}")
        raise _exhausted(resolver, host_info, attempts, last_exc)
    helper_ctx = AuthContext(
        token=credential.password,
        source="git-credential-fill",
        token_type="unknown",  # noqa: S106 - token kind label, not a secret
        host_info=host_info,
        git_env={},
        auth_scheme="basic",
        username=credential.username,
    )
    ok, value = _attempt(STEP_HELPER, helper_ctx)
    if ok:
        return value
    raise _exhausted(resolver, host_info, attempts, value)
