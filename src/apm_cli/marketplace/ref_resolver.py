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

import base64
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass

from ..utils.github_host import (
    build_ado_bearer_git_env,
    build_ado_ssh_url,
    build_authorization_header_git_env,
    build_https_clone_url,
    build_ssh_url,
    default_host,
    is_ado_auth_failure_signal,
    is_azure_devops_hostname,
)
from ._git_utils import redact_token as _redact_token
from .errors import GitLsRemoteError, OfflineMissError
from .git_stderr import translate_git_stderr

__all__ = [
    "RefCache",
    "RefResolver",
    "RemoteRef",
]

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _ado_coordinates_from_owner_repo(
    *,
    host: str,
    owner_repo: str,
) -> tuple[str, str, str]:
    """Return validated ADO coordinates from the canonical dependency owner."""
    from apm_cli.models.dependency.reference import DependencyReference

    try:
        return DependencyReference.canonical_ado_coordinates(host, owner_repo)
    except ValueError as exc:
        if "/_git/" in owner_repo:
            try:
                dep_ref = DependencyReference.parse(f"https://{host}/{owner_repo}")
                return DependencyReference.canonical_ado_coordinates(
                    dep_ref.host,
                    dep_ref.repo_url,
                )
            except ValueError:
                pass
        raise GitLsRemoteError(
            package=owner_repo,
            summary="Azure DevOps resolution requires org/project/repo coordinates.",
            hint=(
                "Re-add the dependency with the original Azure DevOps URL "
                "to regenerate the lock entry."
            ),
        ) from exc


def _ado_remote_path_for_coordinates(
    organization: str,
    project: str,
    repo: str,
) -> str:
    """Return the canonical HTTPS path for ADO coordinates."""
    quoted_org = urllib.parse.quote(organization, safe="")
    quoted_project = urllib.parse.quote(project, safe="")
    quoted_repo = urllib.parse.quote(repo, safe="")
    return f"/{quoted_org}/{quoted_project}/_git/{quoted_repo}"


@dataclass(frozen=True)
class RemoteRef:
    """A single ref returned by ``git ls-remote``."""

    name: str  # e.g. "refs/tags/v1.2.0" or "refs/heads/main"
    sha: str  # 40-char hex SHA


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_DEFAULT_TTL_SECONDS = 300.0  # 5 minutes


@dataclass
class _CacheEntry:
    refs: list[RemoteRef]
    timestamp: float


class RefCache:
    """In-memory cache keyed on the effective remote identity.

    TTL defaults to 5 minutes.  Not thread-safe on its own; callers
    should use external synchronisation (``RefResolver`` does this via
    a per-remote lock).
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, _CacheEntry] = {}

    def get(self, owner_repo: str) -> list[RemoteRef] | None:
        """Return cached refs or ``None`` on miss / expiry."""
        entry = self._store.get(owner_repo)
        if entry is None:
            return None
        if (time.monotonic() - entry.timestamp) > self._ttl:
            del self._store[owner_repo]
            return None
        return list(entry.refs)

    def put(self, owner_repo: str, refs: list[RemoteRef]) -> None:
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


def _parse_ls_remote_output(output: str) -> list[RemoteRef]:
    """Parse ``git ls-remote`` stdout into a list of ``RemoteRef``."""
    refs: list[RemoteRef] = []
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
        Optional PAT or bearer credential. Basic credentials are embedded in
        the URL; ADO bearer credentials are sent through ``http.extraheader``.
    auth_scheme:
        ``"basic"`` (default) or ``"bearer"`` from ``AuthContext``.
    transport_scheme:
        Primary transport selected by ``TransportSelector``. ``"ssh"`` uses
        the SSH URL builder and removes HTTP authorization channels; every
        other value preserves the existing HTTPS behavior.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        offline: bool = False,
        stderr_translator_enabled: bool = True,
        host: str | None = None,
        token: str | None = None,
        auth_scheme: str = "basic",
        git_env: dict[str, str] | None = None,
        auth_resolver=None,
        auth_target=None,
        transport_scheme: str = "https",
        ssh_user: str = "git",
        port: int | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._offline = offline
        self._stderr_translator = stderr_translator_enabled
        self._host: str = host or default_host() or "github.com"
        self._token: str | None = token
        self._auth_scheme = auth_scheme
        self._git_env = dict(git_env) if git_env is not None else None
        self._auth_resolver = auth_resolver
        self._auth_target = auth_target
        self._transport_scheme = transport_scheme
        self._ssh_user = ssh_user
        self._port = port
        self._cache = RefCache()
        self._lock = threading.Lock()
        # Per-remote locks to serialise calls to the same remote while
        # allowing different remotes to proceed in parallel.
        self._remote_locks: dict[str, threading.Lock] = {}

    @property
    def cache(self) -> RefCache:
        """Expose cache for testing."""
        return self._cache

    def _remote_lock(self, owner_repo: str) -> threading.Lock:
        with self._lock:
            if owner_repo not in self._remote_locks:
                self._remote_locks[owner_repo] = threading.Lock()
            return self._remote_locks[owner_repo]

    def _git_url_and_env(
        self,
        owner_repo: str,
        *,
        remote_url: str | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Build the remote URL and auth environment for one git operation."""
        use_ssh = self._transport_scheme == "ssh"
        requested_bearer = self._auth_scheme == "bearer"
        ado_host = is_azure_devops_hostname(self._host)
        if requested_bearer and not ado_host:
            raise GitLsRemoteError(
                package="",
                summary=f"Bearer authentication is not supported for host '{self._host}'.",
                hint="Use bearer authentication only with an Azure DevOps host.",
            )
        bearer = requested_bearer and ado_host and not use_ssh
        url_token = None if requested_bearer or use_ssh else self._token
        if use_ssh and ado_host:
            org, project, repo = _ado_coordinates_from_owner_repo(
                host=self._host,
                owner_repo=owner_repo,
            )
            ssh_host = "ssh.dev.azure.com" if self._host == "dev.azure.com" else self._host
            url = build_ado_ssh_url(org, project, repo, host=ssh_host)
        elif use_ssh:
            url = build_ssh_url(
                self._host,
                owner_repo,
                port=self._port,
                user=self._ssh_user,
            )
        elif remote_url is not None:
            parsed_remote = urllib.parse.urlparse(remote_url)
            expected_ado_path = (
                _ado_remote_path_for_coordinates(
                    *_ado_coordinates_from_owner_repo(host=self._host, owner_repo=owner_repo)
                )
                if ado_host
                else None
            )
            # urlparse lowercases hostname per RFC 3986 3.2.2; normalize both sides.
            if (
                not ado_host
                or parsed_remote.scheme != "https"
                or parsed_remote.hostname != self._host.lower()
                or parsed_remote.path != expected_ado_path
                or parsed_remote.username is not None
                or parsed_remote.password is not None
                or parsed_remote.query
                or parsed_remote.fragment
            ):
                raise GitLsRemoteError(
                    package=owner_repo,
                    summary=(
                        "The canonical remote URL does not match the configured host "
                        "or Azure DevOps dependency coordinates."
                    ),
                    hint=(
                        "Re-add the dependency with the original Azure DevOps URL "
                        "to regenerate the lock entry."
                    ),
                )
            # ADO HTTPS intentionally keeps credentials out of the URL; auth
            # is injected below through git http.extraheader.
            url = remote_url
        elif ado_host:
            url = f"https://{self._host}/{owner_repo}"
        else:
            url = build_https_clone_url(
                self._host,
                owner_repo,
                token=url_token,
                port=self._port,
            )
        if self._git_env is not None:
            env = dict(self._git_env)
        else:
            from apm_cli.core.auth import AuthResolver

            host_kind = "ado" if ado_host else "github"
            env = AuthResolver._build_git_env(
                self._token,
                scheme=self._auth_scheme,
                host_kind=host_kind,
            )
        if use_ssh:
            from apm_cli.core.auth import AuthResolver

            AuthResolver._clear_git_auth_env(env)
            env.pop("GIT_ASKPASS", None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        if not use_ssh:
            env["GIT_ASKPASS"] = "echo"
        if bearer and self._token:
            env.pop("GIT_TOKEN", None)
            env.update(build_ado_bearer_git_env(self._token))
        elif ado_host and url_token:
            credential = base64.b64encode(f":{url_token}".encode()).decode()
            env.update(build_authorization_header_git_env("Basic", credential))
        return url, env

    def list_remote_refs(
        self,
        owner_repo: str,
        *,
        remote_url: str | None = None,
    ) -> list[RemoteRef]:
        """Fetch all tags and heads from the configured Git host.

        Results are cached; subsequent calls for the same remote return
        the cached value until the TTL expires.

        Parameters
        ----------
        owner_repo:
            ``"owner/repo"`` string (no host, no ``.git`` suffix).
        remote_url:
            Canonical ADO URL from ``DependencyReference.to_github_url``.

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
        cache_key = remote_url or owner_repo
        lock = self._remote_lock(cache_key)
        with lock:
            # Check cache first
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            if self._offline:
                raise OfflineMissError(package="", remote=cache_key)

            url, env = self._git_url_and_env(owner_repo, remote_url=remote_url)
            try:
                result = subprocess.run(
                    ["git", "ls-remote", "--tags", "--heads", url],
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                raise GitLsRemoteError(  # noqa: B904
                    package="",
                    summary=f"git ls-remote timed out after {self._timeout}s for '{owner_repo}'.",
                    hint="Increase --timeout or check your network connection.",
                )
            except OSError as exc:
                raise GitLsRemoteError(  # noqa: B904
                    package="",
                    summary=f"Failed to run git ls-remote for '{owner_repo}'.",
                    hint=f"Ensure git is installed and on PATH. Error: {exc}",
                )

            fallback_refs = self._retry_rejected_ado_pat(
                result,
                owner_repo,
                remote_url=remote_url,
            )
            if fallback_refs is not None:
                self._cache.put(cache_key, fallback_refs)
                return fallback_refs

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
            self._cache.put(cache_key, refs)
            return refs

    def _retry_rejected_ado_pat(
        self,
        result: subprocess.CompletedProcess,
        owner_repo: str,
        *,
        remote_url: str | None = None,
    ) -> list[RemoteRef] | None:
        """Retry one rejected ADO basic credential with an Azure CLI bearer."""
        eligible = (
            result.returncode != 0
            and self._auth_resolver is not None
            and self._auth_target is not None
            and self._auth_scheme == "basic"
            and bool(self._token)
            and is_azure_devops_hostname(self._host)
            and self._transport_scheme != "ssh"
        )
        if not eligible:
            return None

        def _bearer_op(bearer: str) -> list[RemoteRef]:
            from apm_cli.core.auth import AuthResolver

            bearer_env = (
                dict(self._git_env) if self._git_env is not None else AuthResolver._build_git_env()
            )
            AuthResolver._clear_git_auth_env(bearer_env)
            bearer_env.update(build_ado_bearer_git_env(bearer))
            bearer_env["GIT_TERMINAL_PROMPT"] = "0"
            bearer_env["GIT_ASKPASS"] = "echo"
            resolver = RefResolver(
                timeout_seconds=self._timeout,
                offline=self._offline,
                stderr_translator_enabled=self._stderr_translator,
                host=self._host,
                token=bearer,
                auth_scheme="bearer",
                git_env=bearer_env,
            )
            try:
                return resolver.list_remote_refs(owner_repo, remote_url=remote_url)
            finally:
                resolver.close()

        fallback = self._auth_resolver.execute_with_bearer_fallback(
            self._auth_target,
            lambda: result,
            _bearer_op,
            lambda outcome: (
                getattr(outcome, "returncode", 0) != 0
                and is_ado_auth_failure_signal(getattr(outcome, "stderr", ""))
            ),
        )
        if isinstance(fallback.outcome, list):
            return fallback.outcome
        return None

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
        url, env = self._git_url_and_env(owner_repo)
        try:
            result = subprocess.run(
                ["git", "ls-remote", url, ref],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise GitLsRemoteError(  # noqa: B904
                package="",
                summary=f"git ls-remote timed out after {self._timeout}s for '{owner_repo}'.",
                hint="Increase --timeout or check your network connection.",
            )
        except OSError as exc:
            raise GitLsRemoteError(  # noqa: B904
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
