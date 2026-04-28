"""Concurrent git ls-remote driver with in-memory ref cache.

``RefResolver`` runs ``git ls-remote`` against GitHub remotes, parses
the output, and caches results in memory (TTL 5 minutes) so that
multiple package entries pointing at the same remote only trigger a
single subprocess call.

Security notes
--------------
* Tokens embedded in ``https://x-access-token:<TOKEN>@`` URLs are
  scrubbed from all error messages and exceptions before they leave
  this module.
* The ``translate_git_stderr`` helper from ``git_stderr.py`` is used
  to classify failures and produce actionable hints.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import GitLsRemoteError, OfflineMissError
from ._git_utils import redact_token as _redact_token
from .git_stderr import translate_git_stderr
from ..utils.github_host import default_host, build_https_clone_url

__all__ = [
    "RemoteRef",
    "RefCache",
    "RefResolver",
]

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class RemoteRef:
    """A single ref returned by ``git ls-remote``."""

    name: str  # e.g. "refs/tags/v1.2.0" or "refs/heads/main"
    sha: str   # 40-char hex SHA


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_DEFAULT_TTL_SECONDS = 300.0  # 5 minutes


@dataclass
class _CacheEntry:
    refs: List[RemoteRef]
    timestamp: float


class RefCache:
    """In-memory cache keyed on ``owner/repo``.

    TTL defaults to 5 minutes.  Not thread-safe on its own; callers
    should use external synchronisation (``RefResolver`` does this via
    a per-remote lock).
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._store: Dict[str, _CacheEntry] = {}

    def get(self, owner_repo: str) -> Optional[List[RemoteRef]]:
        """Return cached refs or ``None`` on miss / expiry."""
        entry = self._store.get(owner_repo)
        if entry is None:
            return None
        if (time.monotonic() - entry.timestamp) > self._ttl:
            del self._store[owner_repo]
            return None
        return list(entry.refs)

    def put(self, owner_repo: str, refs: List[RemoteRef]) -> None:
        """Store *refs* for *owner_repo*."""
        self._store[owner_repo] = _CacheEntry(
            refs=list(refs),
            timestamp=time.monotonic(),
        )

    def clear(self) -> None:
        """Drop all entries."""
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _parse_ls_remote_output(output: str) -> List[RemoteRef]:
    """Parse ``git ls-remote`` stdout into a list of ``RemoteRef``."""
    refs: List[RemoteRef] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        sha, refname = parts[0].strip(), parts[1].strip()
        if not _SHA_RE.match(sha):
            continue
        # Skip peeled tag objects (^{})
        if refname.endswith("^{}"):
            continue
        refs.append(RemoteRef(name=refname, sha=sha))
    return refs


class RefResolver:
    """Run ``git ls-remote`` and cache the results.

    Parameters
    ----------
    timeout_seconds:
        Per-call subprocess timeout.
    offline:
        When ``True``, only return cached refs; never call ``git``.
    stderr_translator_enabled:
        When ``True`` (default), stderr from failed ``git`` calls is
        classified via ``translate_git_stderr``.
    token:
        Optional GitHub PAT to embed in the ``https://`` URL.  When set
        the URL uses ``x-access-token`` authentication; when ``None``
        (default) git runs unauthenticated.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        offline: bool = False,
        stderr_translator_enabled: bool = True,
        host: Optional[str] = None,
        token: Optional[str] = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._offline = offline
        self._stderr_translator = stderr_translator_enabled
        self._host: str = host or default_host() or "github.com"
        self._token: Optional[str] = token
        self._cache = RefCache()
        self._lock = threading.Lock()
        # Per-remote locks to serialise calls to the same remote while
        # allowing different remotes to proceed in parallel.
        self._remote_locks: Dict[str, threading.Lock] = {}

    @property
    def cache(self) -> RefCache:
        """Expose cache for testing."""
        return self._cache

    def _remote_lock(self, owner_repo: str) -> threading.Lock:
        with self._lock:
            if owner_repo not in self._remote_locks:
                self._remote_locks[owner_repo] = threading.Lock()
            return self._remote_locks[owner_repo]

    def list_remote_refs(self, owner_repo: str) -> List[RemoteRef]:
        """Fetch all tags and heads from the configured Git host.

        Results are cached; subsequent calls for the same remote return
        the cached value until the TTL expires.

        Parameters
        ----------
        owner_repo:
            ``"owner/repo"`` string (no host, no ``.git`` suffix).

        Returns
        -------
        list[RemoteRef]
            Parsed refs (tags + heads).

        Raises
        ------
        OfflineMissError
            In offline mode when the cache has no entry.
        GitLsRemoteError
            When the ``git ls-remote`` subprocess fails.
        """
        lock = self._remote_lock(owner_repo)
        with lock:
            # Check cache first
            cached = self._cache.get(owner_repo)
            if cached is not None:
                return cached

            if self._offline:
                raise OfflineMissError(package="", remote=owner_repo)

            url = build_https_clone_url(self._host, owner_repo, token=self._token)
            if not url.endswith(".git"):
                url += ".git"
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
            try:
                result = subprocess.run(
                    ["git", "ls-remote", "--tags", "--heads", url],
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                raise GitLsRemoteError(
                    package="",
                    summary=f"git ls-remote timed out after {self._timeout}s for '{owner_repo}'.",
                    hint="Increase --timeout or check your network connection.",
                )
            except OSError as exc:
                raise GitLsRemoteError(
                    package="",
                    summary=f"Failed to run git ls-remote for '{owner_repo}'.",
                    hint=f"Ensure git is installed and on PATH. Error: {exc}",
                )

            if result.returncode != 0:
                stderr = _redact_token(result.stderr)
                if self._stderr_translator:
                    translated = translate_git_stderr(
                        stderr,
                        exit_code=result.returncode,
                        operation="ls-remote",
                        remote=owner_repo,
                    )
                    raise GitLsRemoteError(
                        package="",
                        summary=translated.summary,
                        hint=translated.hint,
                    )
                raise GitLsRemoteError(
                    package="",
                    summary=f"git ls-remote failed for '{owner_repo}' (exit {result.returncode}).",
                    hint=_redact_token(stderr[:200]) if stderr else "No stderr output.",
                )

            refs = _parse_ls_remote_output(result.stdout)
            self._cache.put(owner_repo, refs)
            return refs

    # -----------------------------------------------------------------
    # Single-ref resolution (no cache)
    # -----------------------------------------------------------------

    def resolve_ref_sha(self, owner_repo: str, ref: str = "HEAD") -> str:
        """Resolve a single ref to its concrete SHA via ``git ls-remote``.

        Unlike ``list_remote_refs`` this queries a single ref and does
        not cache the result (the caller typically stores the SHA
        immediately).

        Parameters
        ----------
        owner_repo:
            ``"owner/repo"`` string (no host, no ``.git`` suffix).
        ref:
            The ref to resolve (default ``"HEAD"``).

        Returns
        -------
        str
            40-char hex SHA.

        Raises
        ------
        GitLsRemoteError
            When the ref does not exist or the subprocess fails.
        """
        url = build_https_clone_url(self._host, owner_repo, token=self._token)
        if not url.endswith(".git"):
            url += ".git"
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
        try:
            result = subprocess.run(
                ["git", "ls-remote", url, ref],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise GitLsRemoteError(
                package="",
                summary=f"git ls-remote timed out after {self._timeout}s for '{owner_repo}'.",
                hint="Increase --timeout or check your network connection.",
            )
        except OSError as exc:
            raise GitLsRemoteError(
                package="",
                summary=f"Failed to run git ls-remote for '{owner_repo}'.",
                hint=f"Ensure git is installed and on PATH. Error: {exc}",
            )

        if result.returncode != 0:
            stderr = _redact_token(result.stderr)
            if self._stderr_translator:
                translated = translate_git_stderr(
                    stderr,
                    exit_code=result.returncode,
                    operation="ls-remote",
                    remote=owner_repo,
                )
                raise GitLsRemoteError(
                    package="",
                    summary=translated.summary,
                    hint=translated.hint,
                )
            raise GitLsRemoteError(
                package="",
                summary=f"git ls-remote failed for '{owner_repo}' (exit {result.returncode}).",
                hint=_redact_token(stderr[:200]) if stderr else "No stderr output.",
            )

        refs = _parse_ls_remote_output(result.stdout)
        if not refs:
            raise GitLsRemoteError(
                package="",
                summary=f"Ref '{ref}' not found on remote '{owner_repo}'.",
                hint="Check that the ref exists and you have access to the repository.",
            )
        return refs[0].sha

    def close(self) -> None:
        """Release resources (cache, locks)."""
        self._cache.clear()
        with self._lock:
            self._remote_locks.clear()
