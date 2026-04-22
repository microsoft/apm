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

import shutil
import subprocess
import threading
from typing import Optional


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
        stderr: Optional[str] = None,
        tenant_id: Optional[str] = None,
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

    def __init__(self, az_command: str = "az") -> None:
        self._az_command = az_command
        self._cache: dict[str, str] = {}
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
        with self._lock:
            cached = self._cache.get(self.ADO_RESOURCE_ID)
            if cached is not None:
                return cached

        # az availability check (outside lock -- no shared-state mutation).
        if not self.is_available():
            raise AzureCliBearerError(
                "az CLI is not installed or not on PATH",
                kind="az_not_found",
            )

        token = self._run_get_access_token()

        with self._lock:
            self._cache[self.ADO_RESOURCE_ID] = token
        return token

    def get_current_tenant_id(self) -> Optional[str]:
        """Return the active Entra tenant ID (best-effort).

        Uses ``az account show --query tenantId -o tsv``.  Returns ``None``
        on any failure -- this method never raises.
        """
        try:
            result = subprocess.run(
                [self._az_command, "account", "show",
                 "--query", "tenantId", "-o", "tsv"],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                tenant = result.stdout.strip()
                if tenant:
                    return tenant
        except Exception:  # noqa: BLE001 -- intentionally broad
            pass
        return None

    def clear_cache(self) -> None:
        """Drop any cached token.

        Useful for tests; rarely needed in production.
        """
        with self._lock:
            self._cache.clear()

    # -- internals ----------------------------------------------------------

    def _run_get_access_token(self) -> str:
        """Shell out to ``az account get-access-token`` and return the JWT.

        Raises AzureCliBearerError on any failure.
        """
        cmd = [
            self._az_command,
            "account",
            "get-access-token",
            "--resource",
            self.ADO_RESOURCE_ID,
            "--query",
            "accessToken",
            "-o",
            "tsv",
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

        token = result.stdout.strip()
        if not _looks_like_jwt(token):
            raise AzureCliBearerError(
                "az CLI returned a response that does not look like a JWT",
                kind="subprocess_error",
                stderr=(result.stderr or "").strip() or None,
            )
        return token


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

_provider_singleton: Optional["AzureCliBearerProvider"] = None
_provider_singleton_lock = threading.Lock()


def get_bearer_provider() -> "AzureCliBearerProvider":
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
