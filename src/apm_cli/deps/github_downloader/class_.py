"""GitHub package downloader for APM dependencies."""

import contextlib
import os
import sys
import threading
from pathlib import Path
from typing import Union

from ...core.auth import AuthResolver
from ...models.apm_package import (
    DependencyReference,
    PackageInfo,
)
from ..download_strategies import DownloadDelegate
from ..transport_selection import (
    ProtocolPreference,
    TransportSelector,
    is_fallback_allowed,
    protocol_pref_from_env,
)

# Public docs anchor for the cross-protocol fallback caveat surfaced by the
# #786 warning. Lives under the dependencies guide, next to the canonical
# `--allow-protocol-fallback` section (Starlight site defined in
# docs/astro.config.mjs).
_PROTOCOL_FALLBACK_DOCS_URL = (
    "https://microsoft.github.io/apm/guides/dependencies/#restoring-the-legacy-permissive-chain"
)


def _debug(message: str) -> None:
    """Print debug message if APM_DEBUG environment variable is set."""
    if os.environ.get("APM_DEBUG"):
        print(f"[DEBUG] {message}", file=sys.stderr)


def _close_repo(repo) -> None:
    """Release GitPython handles so directories can be deleted on Windows."""
    if repo is None:
        return
    with contextlib.suppress(Exception):
        repo.git.clear_cache()
    with contextlib.suppress(Exception):
        repo.close()


def _rmtree(path) -> None:
    """Remove a directory tree, handling read-only files and brief Windows locks.

    Delegates to :func:`robust_rmtree` which retries with exponential backoff
    on transient lock errors (e.g. antivirus scanning on Windows).
    """
    from ...utils.file_ops import robust_rmtree

    robust_rmtree(path, ignore_errors=True)


from .artifactory import _ArtifactoryMixin
from .auth_helpers import _AuthHelpersMixin
from .bare_clone import _BareCloneMixin
from .git_env import _GitEnvMixin
from .transport_plan import _TransportPlanMixin


class GitHubPackageDownloader(
    _ArtifactoryMixin, _AuthHelpersMixin, _BareCloneMixin, _GitEnvMixin, _TransportPlanMixin
):
    def __init__(
        self,
        auth_resolver=None,
        transport_selector: TransportSelector | None = None,
        protocol_pref: ProtocolPreference | None = None,
        allow_fallback: bool | None = None,
    ):
        """Initialize the GitHub package downloader.

        Args:
            auth_resolver: Auth resolver instance. Defaults to a new AuthResolver.
            transport_selector: TransportSelector for protocol decisions.
                Defaults to a new selector with GitConfigInsteadOfResolver.
            protocol_pref: User-stated transport preference for shorthand
                deps. When None, reads APM_GIT_PROTOCOL env.
            allow_fallback: When True, permits cross-protocol fallback
                (legacy behavior). When None, reads
                APM_ALLOW_PROTOCOL_FALLBACK env.
        """
        self.auth_resolver = auth_resolver or AuthResolver()
        self.token_manager = self.auth_resolver._token_manager  # Backward compat
        self.git_env = self._setup_git_environment()
        self._transport_selector = transport_selector or TransportSelector()
        self._protocol_pref = (
            protocol_pref if protocol_pref is not None else protocol_pref_from_env()
        )
        self._allow_fallback = (
            allow_fallback if allow_fallback is not None else is_fallback_allowed()
        )
        # Dedup set for the issue #786 cross-protocol port warning: one install
        # run calls _clone_with_fallback multiple times per dep (ref-resolution
        # clone, then the actual dep clone). We want the warning exactly once
        # per (host, repo, port) identity across all those calls.
        self._fallback_port_warned: set = set()
        self._fallback_port_warned_lock = threading.Lock()

        # Delegate backend-specific download logic to the download delegate.
        self._strategies = DownloadDelegate(host=self)

        # Artifactory orchestration is encapsulated in a dedicated facade
        # (download_package / download_subdirectory) backed by the
        # DownloadDelegate's HTTP archive downloader.
        from ..artifactory_orchestrator import ArtifactoryOrchestrator
        from ..clone_engine import CloneEngine
        from ..git_reference_resolver import GitReferenceResolver

        self._artifactory = ArtifactoryOrchestrator(archive_downloader=self._strategies)
        self._refs = GitReferenceResolver(host=self)
        self._clone_engine = CloneEngine(host=self)

        # #1369: tiered ref resolver. Attached by resolve.py / outdated.py
        # after construction via ``build_tiered_ref_resolver``. When set,
        # :meth:`resolve_git_reference` delegates to it before falling
        # through to ``self._refs.resolve``. Declared here so the
        # attribute is part of the documented downloader surface rather
        # than a monkey-patched field.
        self._tiered_resolver = None

        # WS2a (#1116): per-run shared clone cache for subdirectory dep
        # deduplication.  Set by the install pipeline before resolution
        # starts; None means no dedup (each subdir dep clones independently).
        self.shared_clone_cache = None

        # WS3 (#1116): persistent cross-run git cache.  When set, the
        # download flow checks the on-disk cache before any network clone.
        # Set by the install pipeline; None disables persistent caching.
        self.persistent_git_cache = None

    @property
    def registry_config(self):
        """Lazily-constructed :class:`~apm_cli.deps.registry_proxy.RegistryConfig`.

        Returns ``None`` when no registry proxy is configured.
        """
        if not hasattr(self, "_registry_config_cache"):
            from ..registry_proxy import RegistryConfig

            self._registry_config_cache = RegistryConfig.from_env()
        return self._registry_config_cache

    def download_virtual_file_package(
        self,
        dep_ref: DependencyReference,
        target_path: Path,
        progress_task_id=None,
        progress_obj=None,
    ) -> PackageInfo:
        return _download_ops.download_virtual_file_package(
            self, dep_ref, target_path, progress_task_id, progress_obj
        )

    def _try_sparse_checkout(
        self,
        dep_ref: DependencyReference,
        temp_clone_path: Path,
        subdir_path: str,
        ref: str | None = None,
    ) -> bool:
        return _download_ops._try_sparse_checkout(self, dep_ref, temp_clone_path, subdir_path, ref)

    def download_subdirectory_package(
        self,
        dep_ref: DependencyReference,
        target_path: Path,
        progress_task_id=None,
        progress_obj=None,
    ) -> PackageInfo:
        return _download_ops.download_subdirectory_package(
            self, dep_ref, target_path, progress_task_id, progress_obj
        )

    def download_package(
        self,
        repo_ref: Union[str, "DependencyReference"],
        target_path: Path,
        progress_task_id=None,
        progress_obj=None,
        verbose_callback=None,
    ) -> PackageInfo:
        from .download_ops import ProgressCtx

        ctx = ProgressCtx(
            progress_task_id=progress_task_id,
            progress_obj=progress_obj,
            verbose_callback=verbose_callback,
        )
        return _download_ops.download_package(self, repo_ref, target_path, ctx)

    def _get_clone_progress_callback(self):
        return _download_ops._get_clone_progress_callback(self)


from . import download_ops as _download_ops
