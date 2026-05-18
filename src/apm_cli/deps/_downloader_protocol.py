"""Downloader collaboration Protocol extracted from git_reference_resolver.py."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..core.auth import AuthResolver
    from ..models.apm_package import DependencyReference, RemoteRef


class _DownloaderContext(Protocol):
    """The slice of :class:`GitHubPackageDownloader` the resolver needs.

    Kept duck-typed (``Protocol``) so tests can inject a minimal stub
    without instantiating the full downloader.
    """

    auth_resolver: AuthResolver
    git_env: dict
    shared_clone_cache: object | None

    def _resolve_dep_token(self, dep_ref: DependencyReference | None = ...) -> str | None: ...
    def _resolve_dep_auth_ctx(self, dep_ref: DependencyReference | None = ...): ...
    def _build_noninteractive_git_env(
        self,
        *,
        preserve_config_isolation: bool = ...,
        suppress_credential_helpers: bool = ...,
    ) -> dict: ...
    def _build_repo_url(
        self,
        repo_url_base: str,
        *,
        use_ssh: bool = ...,
        dep_ref: DependencyReference | None = ...,
        token: str | None = ...,
        auth_scheme: str = ...,
    ) -> str: ...
    def _clone_with_fallback(self, *args, **kwargs): ...
    def _sanitize_git_error(self, error_message: str) -> str: ...
    def _resilient_get(self, url: str, headers: dict, timeout: int = ...): ...
    def _parse_ls_remote_output(self, output: str) -> list[RemoteRef]: ...
    def _sort_remote_refs(self, refs: list[RemoteRef]) -> list[RemoteRef]: ...
    def _parse_artifactory_base_url(self) -> tuple | None: ...
    def _should_use_artifactory_proxy(self, dep_ref: DependencyReference) -> bool: ...
