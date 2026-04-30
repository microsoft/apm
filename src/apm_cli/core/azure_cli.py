"""Azure CLI bearer-token acquisition for Azure DevOps authentication.

Acquires Entra ID bearer tokens from the ``az`` CLI for use with Azure
DevOps Git operations.  Tokens are cached in-memory per process keyed by
resource GUID.

First call: ~200-500 ms (subprocess spawn).  Subsequent calls: in-memory.
No on-disk cache (token TTL is ~1 h, not worth the complexity).

The provider never invokes ``az login`` -- interactive auth is the user's
responsibility.  APM is a package manager, not an auth broker.

Usage::

    provider = AzureCliBearerProvider()
    if provider.is_available():
        token = provider.get_bearer_token()  # JWT string
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Tuple  # noqa: F401, UP035

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AzureCliBearerError(Exception):
    """Raised when az CLI bearer-token acquisition fails.

    Attributes:
        kind:      Failure category -- one of ``"az_not_found"``,
                   ``"not_logged_in"``, ``"subprocess_error"``.
        stderr:    Captured stderr from the ``az`` subprocess, if any.
        tenant_id: Active Entra tenant ID, if it could be determined.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        stderr: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.stderr = stderr
        self.tenant_id = tenant_id


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

_SUBPROCESS_TIMEOUT_SECONDS = 30


class AzureCliBearerProvider:
    """Acquires Entra ID bearer tokens for Azure DevOps via the az CLI.

    Tokens are cached in-memory per process keyed by resource GUID.
    First call: ~200-500 ms (subprocess spawn).  Subsequent calls: in-memory.
    No on-disk cache (token TTL is ~1 h, not worth the complexity).

    The provider never invokes ``az login`` -- interactive auth is the user's
    responsibility.  APM is a package manager, not an auth broker.
    """

    ADO_RESOURCE_ID: str = "499b84ac-1321-427f-aa17-267ca6975798"

    # Refresh slack: refresh tokens this many seconds before their actual expiry
    # so that an in-flight request never gets HTTP 401 on a token we considered
    # "fresh" 100ms ago.
    _EXPIRY_SLACK_SECONDS: int = 60

    def __init__(self, az_command: str = "az") -> None:
        self._az_command = az_command
        # Cache stores (token, expires_at_epoch_seconds). expires_at is None
        # if the response did not include an expiresOn field (very old az
        # versions); in that case the token is treated as never-expiring
        # within this process, matching the prior behaviour.
        self._cache: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.Lock()

    # -- public API ---------------------------------------------------------

    def is_available(self) -> bool:
        """Return True iff the ``az`` binary is on PATH.

        Does NOT check whether the user is logged in -- that requires a
        subprocess call and is deferred to :meth:`get_bearer_token`.
        """
        return shutil.which(self._az_command) is not None

    def get_bearer_token(self) -> str:
        """Acquire (or return cached) bearer token for Azure DevOps.

        Returns:
            A JWT access token string.

        Raises:
            AzureCliBearerError: With ``kind`` set to one of:

                - ``"az_not_found"``     -- ``az`` binary not on PATH.
                - ``"not_logged_in"``    -- ``az`` returned exit code != 0;
                  the user must run ``az login``.
                - ``"subprocess_error"`` -- some other subprocess failure
                  (timeout, signal, malformed response).
        """
        # C7/F4 #852: singleflight via lock-held subprocess. Holding the lock
        # across the (potentially 200-500 ms) `az` invocation means concurrent
        # callers wait for the first one to populate the cache instead of all
        # spawning their own subprocess. APM is a CLI -- this contention is
        # rare in practice, and the simplicity is worth the brief stall.
        with self._lock:
            cached = self._cache.get(self.ADO_RESOURCE_ID)
            if cached is not None:
                token, expires_at = cached
                if expires_at is None or expires_at > time.time():
                    return token
                # Expired -- fall through to refresh under the same lock.

            # az availability check (also under lock so we don't race with
            # a hypothetical clear_cache + chdir/PATH change in another thread).
            if not self.is_available():
                raise AzureCliBearerError(
                    "az CLI is not installed or not on PATH",
                    kind="az_not_found",
                )

            token, expires_at = self._run_get_access_token()
            self._cache[self.ADO_RESOURCE_ID] = (token, expires_at)
            return token

    def get_current_tenant_id(self) -> str | None:
        """Return the active Entra tenant ID (best-effort).

        Uses ``az account show --query tenantId -o tsv``.  Returns ``None``
        on any failure -- this method never raises.
        """
        try:
            result = subprocess.run(
                [self._az_command, "account", "show", "--query", "tenantId", "-o", "tsv"],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                tenant = result.stdout.strip()
                if tenant:
                    return tenant
        except Exception:
            pass
        return None

    def clear_cache(self) -> None:
        """Drop any cached token.

        Useful for tests; rarely needed in production.
        """
        with self._lock:
            self._cache.clear()

    # -- internals ----------------------------------------------------------

    def _run_get_access_token(self) -> tuple[str, float | None]:
        """Shell out to ``az account get-access-token`` and return ``(jwt, expires_at)``.

        ``expires_at`` is the absolute epoch-second timestamp at which the
        token expires (already adjusted by ``_EXPIRY_SLACK_SECONDS`` so callers
        can use a strict ``> time.time()`` comparison). It may be ``None`` if
        the az version in use does not include ``expiresOn`` in JSON output --
        in which case the token is treated as never-expiring within this
        process (the prior behaviour).

        Raises AzureCliBearerError on any failure.
        """
        # F4 #852: query JSON so we can read both accessToken and expiresOn.
        cmd = [
            self._az_command,
            "account",
            "get-access-token",
            "--resource",
            self.ADO_RESOURCE_ID,
            "-o",
            "json",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise AzureCliBearerError(
                f"az CLI timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s",
                kind="subprocess_error",
                stderr=str(exc),
            ) from exc
        except OSError as exc:
            raise AzureCliBearerError(
                f"Failed to execute az CLI: {exc}",
                kind="subprocess_error",
                stderr=str(exc),
            ) from exc

        if result.returncode != 0:
            stderr_text = (result.stderr or "").strip()
            raise AzureCliBearerError(
                f"az CLI returned exit code {result.returncode}: {stderr_text}",
                kind="not_logged_in",
                stderr=stderr_text,
            )

        raw = (result.stdout or "").strip()
        token: str = ""
        expires_at: float | None = None
        # Try JSON first (modern az). Fall back to treating stdout as a bare
        # JWT for backwards compatibility (very old az or unusual configs).
        try:
            payload = json.loads(raw)
            token = (payload.get("accessToken") or "").strip()
            expires_on = payload.get("expiresOn") or payload.get("expires_on")
            if isinstance(expires_on, str) and expires_on:
                expires_at = _parse_expires_on(expires_on)
        except (json.JSONDecodeError, AttributeError, TypeError):
            token = raw

        if not _looks_like_jwt(token):
            raise AzureCliBearerError(
                "az CLI returned a response that does not look like a JWT",
                kind="subprocess_error",
                stderr=(result.stderr or "").strip() or None,
            )
        if expires_at is not None:
            expires_at -= self._EXPIRY_SLACK_SECONDS
        return token, expires_at


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Module-level singleton (B3 #852)
# ---------------------------------------------------------------------------
#
# AzureCliBearerProvider advertises an in-memory token cache, but every fresh
# instantiation gets an empty cache, so per-callsite construction defeats the
# design. Use get_bearer_provider() everywhere to share one cache across the
# process. Tests can call .clear_cache() on the returned singleton.

_provider_singleton: AzureCliBearerProvider | None = None
_provider_singleton_lock = threading.Lock()


def get_bearer_provider() -> AzureCliBearerProvider:
    """Return the process-wide AzureCliBearerProvider singleton."""
    global _provider_singleton
    if _provider_singleton is None:
        with _provider_singleton_lock:
            if _provider_singleton is None:
                _provider_singleton = AzureCliBearerProvider()
    return _provider_singleton


def _looks_like_jwt(value: str) -> bool:
    """Return True if *value* loosely resembles a JWT.

    A real JWT is three base64url segments separated by dots.  We only
    check the prefix and minimum length -- full validation is the
    server's job.
    """
    return value.startswith("eyJ") and len(value) > 100


def _parse_expires_on(value: str) -> float | None:
    """Parse an ``expiresOn`` field from ``az account get-access-token`` JSON.

    Accepts both forms emitted by various az versions:
      * ISO-8601 with timezone, e.g. ``"2025-01-15T08:30:00.000000+00:00"``.
      * Local-naive datetime, e.g. ``"2025-01-15 08:30:00.000000"`` (older az).

    Returns the absolute epoch seconds (UTC), or ``None`` on parse failure.
    Local-naive timestamps are interpreted as the local timezone since that
    is what those az versions emit.
    """
    raw = value.strip()
    if not raw:
        return None
    # Normalize ISO 8601 separator if present.
    candidate = raw.replace(" ", "T", 1) if " " in raw and "T" not in raw else raw
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # az emits naive timestamps in *local* time on older versions; respect that.
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).timestamp()
