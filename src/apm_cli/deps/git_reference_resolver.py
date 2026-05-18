"""Git reference resolution.

Resolves user-supplied refs (branch / tag / commit SHA / unspecified)
to a concrete :class:`ResolvedReference` and enumerates remote refs
without cloning. Splits three concerns out of the
:class:`GitHubPackageDownloader` monolith:

1. **Cheap SHA resolution** for GitHub-family hosts via the commits API
   (``GET /repos/.../commits/{ref}`` with ``Accept: application/vnd.github.sha``).
2. **List remote refs** via ``git ls-remote --tags --heads`` with the
   ADO bearer-fallback dance handled by ``AuthResolver``.
3. **Resolve a ref to a SHA** via clone-and-introspect (shallow first,
   then full clone fallback) when the cheap path does not apply.

Design pattern: **Strategy**, exposed through a single :class:`Facade`
(:class:`GitReferenceResolver`).

The resolver holds a reference to the surrounding downloader (a
``DownloaderContext`` Protocol) so it can reuse shared infrastructure
(auth env, transport-aware clone, ls-remote parsing helpers) without
duplicating that code. This mirrors the existing ``DownloadDelegate``
pattern.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from git.exc import GitCommandError

from ..models.apm_package import (
    DependencyReference,
    GitReferenceType,
    RemoteRef,
    ResolvedReference,
)
from ..utils.github_host import (
    default_host,
    is_ado_auth_failure_signal,
    is_github_hostname,
)

if TYPE_CHECKING:
    from ..core.auth import AuthResolver


# ---------------------------------------------------------------------------
# Downloader collaboration contract
# ---------------------------------------------------------------------------


from ._downloader_protocol import _DownloaderContext  # noqa: E402

# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class GitReferenceResolver:
    """Resolve and enumerate Git references for an APM dependency."""

    def __init__(self, host: _DownloaderContext) -> None:
        self._host = host

    # -- list_remote_refs ----------------------------------------------

    def list_remote_refs(self, dep_ref: DependencyReference) -> list[RemoteRef]:
        """Enumerate remote tags and branches without cloning.

        Uses ``git ls-remote --tags --heads`` for all git hosts (GitHub,
        Azure DevOps, GitLab, generic). Artifactory dependencies return
        an empty list (no git repo).
        """
        host = self._host

        if dep_ref.is_artifactory():
            return []

        is_ado = dep_ref.is_azure_devops()
        dep_token = host._resolve_dep_token(dep_ref)
        dep_auth_ctx = host._resolve_dep_auth_ctx(dep_ref)
        dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"

        repo_url_base = dep_ref.repo_url

        ls_env = self._build_ls_remote_env(dep_ref, dep_token, dep_auth_ctx)

        remote_url = host._build_repo_url(
            repo_url_base,
            use_ssh=False,
            dep_ref=dep_ref,
            token=dep_token,
            auth_scheme=dep_auth_scheme,
        )

        # Route through the github_downloader module so that test patches
        # of ``apm_cli.deps.github_downloader.git.cmd.Git`` intercept here.
        from . import github_downloader as _gd

        g = _gd.git.cmd.Git()

        def _primary_op():
            try:
                output = g.ls_remote("--tags", "--heads", remote_url, env=ls_env)
                return ("ok", output)
            except GitCommandError as exc:
                return ("err", exc)

        def _bearer_op(bearer):
            # SECURITY: _build_git_env(scheme="bearer") yields a clean env
            # (no leaked PAT). JWT travels via http.extraHeader.
            bearer_env = host.auth_resolver._build_git_env(bearer, scheme="bearer", host_kind="ado")
            bearer_url = host._build_repo_url(
                repo_url_base,
                use_ssh=False,
                dep_ref=dep_ref,
                token=None,
                auth_scheme="bearer",
            )
            try:
                output = g.ls_remote("--tags", "--heads", bearer_url, env=bearer_env)
                return ("ok", output)
            except GitCommandError as exc:
                return ("err", exc)

        def _is_auth_failure(outcome):
            if outcome is None or outcome[0] != "err":
                return False
            return is_ado_auth_failure_signal(str(outcome[1]))

        ado_eligible = is_ado and dep_auth_scheme == "basic" and dep_token is not None

        if ado_eligible:
            fb = host.auth_resolver.execute_with_bearer_fallback(
                dep_ref, _primary_op, _bearer_op, _is_auth_failure
            )
            outcome = fb.outcome
            ado_bearer_also_failed = fb.bearer_attempted and _is_auth_failure(outcome)
        else:
            outcome = _primary_op()
            ado_bearer_also_failed = False

        if outcome[0] == "ok":
            refs = host._parse_ls_remote_output(outcome[1])
            return host._sort_remote_refs(refs)

        e = outcome[1]
        dep_host = dep_ref.host
        error_msg = self._format_list_refs_error(
            e, dep_ref, dep_host, repo_url_base, ado_bearer_also_failed
        )
        raise RuntimeError(error_msg) from e

    # -- list_remote_refs helpers ------------------------------------

    def _build_ls_remote_env(
        self,
        dep_ref: DependencyReference,
        dep_token: str | None,
        dep_auth_ctx: object,
    ) -> dict:
        """Return the git environment dict for ``git ls-remote``."""
        host = self._host
        dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"
        if dep_token:
            if dep_auth_scheme == "bearer" and dep_auth_ctx is not None:
                return dep_auth_ctx.git_env
            return host.git_env
        return host._build_noninteractive_git_env(
            preserve_config_isolation=bool(getattr(dep_ref, "is_insecure", False)),
            suppress_credential_helpers=bool(getattr(dep_ref, "is_insecure", False)),
        )

    def _format_list_refs_error(
        self,
        exc: Exception,
        dep_ref: DependencyReference,
        dep_host: str | None,
        repo_url_base: str,
        ado_also_failed: bool,
    ) -> str:
        """Build the error message string when ``git ls-remote`` fails."""
        host = self._host
        is_ado = dep_ref.is_azure_devops()
        is_github = is_github_hostname(dep_host) if dep_host else True
        is_generic = not is_ado and not is_github
        error_msg = f"Failed to list remote refs for {repo_url_base}. "
        if is_generic:
            if dep_host:
                host_info = host.auth_resolver.classify_host(dep_host, port=dep_ref.port)
                host_name = host_info.display_name
            else:
                host_name = "the target host"
            error_msg += (
                f"For private repositories on {host_name}, configure SSH keys "
                f"or a git credential helper. "
                f"APM delegates authentication to git for non-GitHub/ADO hosts."
            )
        else:
            target_host = dep_host or default_host()
            org = repo_url_base.split("/")[0] if repo_url_base else None
            error_msg += host.auth_resolver.build_error_context(
                target_host,
                "list refs",
                org=org,
                port=dep_ref.port if dep_ref else None,
                dep_url=dep_ref.repo_url if dep_ref else None,
                bearer_also_failed=ado_also_failed,
            )
        sanitized = host._sanitize_git_error(str(exc))
        error_msg += f" Last error: {sanitized}"
        return error_msg

    # -- resolve_commit_sha_for_ref ------------------------------------

    def resolve_commit_sha_for_ref(self, dep_ref: DependencyReference, ref: str) -> str | None:
        """Resolve a Git ref to a 40-char SHA via the cheap GitHub commits API.

        Returns the SHA on success, or ``None`` on any failure (404,
        network, non-GitHub host, unexpected body shape, etc.).
        Failures are swallowed so callers can still record the ref name.
        """
        host = self._host

        try:
            if dep_ref.is_artifactory() or dep_ref.is_azure_devops():
                return None
        except Exception:
            return None

        target_host = dep_ref.host or default_host()

        if re.match(r"^[a-f0-9]{40}$", ref.lower() or ""):
            return ref.lower()

        try:
            dep_ref.repo_url.split("/", 1)
        except (AttributeError, ValueError):
            return None

        from .host_backends import backend_for

        backend = backend_for(dep_ref, host.auth_resolver, fallback_host=target_host)
        api_url = backend.build_commits_api_url(dep_ref, ref)
        if api_url is None:
            return None

        return self._fetch_sha_from_api(api_url, target_host, dep_ref)

    # -- resolve_commit_sha_for_ref helper ---------------------------

    def _fetch_sha_from_api(
        self,
        api_url: str,
        target_host: str,
        dep_ref: DependencyReference,
    ) -> str | None:
        """Perform the HTTP call and parse the 40-char SHA from the response."""
        host = self._host
        org = None
        parts = dep_ref.repo_url.split("/")
        if parts:
            org = parts[0]
        try:
            file_ctx = host.auth_resolver.resolve(target_host, org, port=dep_ref.port)
            token = file_ctx.token
        except Exception:
            token = None
        headers: dict[str, str] = {"Accept": "application/vnd.github.sha"}
        if token:
            headers["Authorization"] = f"token {token}"
        try:
            response = host._resilient_get(api_url, headers=headers, timeout=10)
            if response.status_code != 200:
                return None
            body = (response.text or "").strip()
            if re.match(r"^[a-f0-9]{40}$", body.lower()):
                return body.lower()
            return None
        except Exception:
            return None

    # -- resolve (clone-and-introspect) --------------------------------

    def resolve(self, repo_ref: str | DependencyReference) -> ResolvedReference:
        """Resolve a Git reference (branch/tag/commit) to a specific commit SHA."""
        from ..config import get_apm_temp_dir
        from .github_downloader import _rmtree

        host = self._host

        if isinstance(repo_ref, DependencyReference):
            dep_ref = repo_ref
        else:
            try:
                dep_ref = DependencyReference.parse(repo_ref)
            except ValueError as e:
                raise ValueError(f"Invalid repository reference '{repo_ref}': {e}")  # noqa: B904

        ref = dep_ref.reference or None
        original_ref_str = str(dep_ref)

        # Artifactory: no git repo to query, return ref-based resolution
        if dep_ref.is_artifactory() or (
            host._parse_artifactory_base_url() and host._should_use_artifactory_proxy(dep_ref)
        ):
            effective_ref = ref or "main"
            is_commit = re.match(r"^[a-f0-9]{7,40}$", effective_ref.lower()) is not None
            return ResolvedReference(
                original_ref=original_ref_str,
                ref_type=GitReferenceType.COMMIT if is_commit else GitReferenceType.BRANCH,
                resolved_commit=None,
                ref_name=effective_ref,
            )

        is_likely_commit = bool(ref) and re.match(r"^[a-f0-9]{7,40}$", ref.lower()) is not None

        temp_dir = None
        try:
            temp_dir = Path(tempfile.mkdtemp(dir=get_apm_temp_dir()))

            if is_likely_commit:
                ref_type, resolved_commit, ref_name = self._resolve_as_commit(
                    dep_ref, ref, temp_dir
                )
            else:
                ref_type, resolved_commit, ref_name = self._resolve_as_branch_or_tag(
                    dep_ref, ref, temp_dir
                )

        finally:
            if temp_dir is not None:
                _rmtree(temp_dir)

        return ResolvedReference(
            original_ref=original_ref_str,
            ref_type=ref_type,
            resolved_commit=resolved_commit,
            ref_name=ref_name,
        )

    # -- resolve helpers -----------------------------------------------

    def _resolve_as_commit(
        self,
        dep_ref: DependencyReference,
        ref: str,
        temp_dir: Path,
    ) -> tuple:
        """Clone the repo and resolve *ref* as a commit SHA.

        Returns ``(ref_type, resolved_commit, ref_name)``.
        """
        host = self._host
        try:
            repo = host._clone_with_fallback(
                dep_ref.repo_url, temp_dir, progress_reporter=None, dep_ref=dep_ref
            )
            commit = repo.commit(ref)
            return GitReferenceType.COMMIT, commit.hexsha, ref
        except Exception as e:
            sanitized_error = host._sanitize_git_error(str(e))
            raise ValueError(  # noqa: B904
                f"Could not resolve commit '{ref}' in repository "
                f"{dep_ref.repo_url}: {sanitized_error}"
            )

    def _resolve_as_branch_or_tag(
        self,
        dep_ref: DependencyReference,
        ref: str | None,
        temp_dir: Path,
    ) -> tuple:
        """Clone the repo (shallow first, full fallback) and resolve *ref*.

        Returns ``(ref_type, resolved_commit, ref_name)``.
        """
        from ._clone_resolver import _resolve_branch_or_tag

        return _resolve_branch_or_tag(self._host, dep_ref, ref, temp_dir)
