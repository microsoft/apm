"""GitHub package downloader for APM dependencies."""

import contextlib
import os
import re
import stat  # noqa: F401
import subprocess
import sys
import tempfile
import threading
import time  # noqa: F401
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Union

import git  # noqa: F401  # re-exported for tests that patch github_downloader.git
import requests
from git import RemoteProgress, Repo
from git.exc import GitCommandError

from ...core.auth import AuthContext, AuthResolver
from ...models.apm_package import (
    APMPackage,
    DependencyReference,
    GitReferenceType,
    PackageInfo,
    PackageType,
    RemoteRef,
    ResolvedReference,
    validate_apm_package,
)
from ...utils.console import _rich_warning  # noqa: F401  # re-exported for tests
from ...utils.github_host import (
    default_host,
    is_azure_devops_hostname,  # noqa: F401
    is_github_hostname,
    sanitize_token_url_in_message,
)
from ...utils.yaml_io import yaml_to_str
from ..bare_cache import (
    bare_clone_with_fallback,
    clone_with_fallback,
    fetch_sha_into_bare,
    materialize_from_bare,
)
from ..download_strategies import DownloadDelegate
from ..git_remote_ops import (
    parse_ls_remote_output,
    semver_sort_key,
    sort_remote_refs,
)
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


class _AuthHelpersMixin:
    def _resolve_dep_token(self, dep_ref: DependencyReference | None = None) -> str | None:
        """Resolve the per-dependency auth token via AuthResolver.

        GitHub, GitLab, and ADO hosts use the token resolved by AuthResolver.
        Other generic hosts return None so git credential helpers can provide
        credentials instead.

        Args:
            dep_ref: Optional dependency reference for host/org lookup.

        Returns:
            Token string or None.
        """
        if dep_ref is None:
            return self.github_token

        if self._is_generic_dependency_host(dep_ref):
            return None

        dep_ctx = self.auth_resolver.resolve_for_dep(dep_ref)
        return dep_ctx.token

    def _resolve_dep_auth_ctx(
        self, dep_ref: DependencyReference | None = None
    ) -> AuthContext | None:
        """Resolve the full AuthContext for a dependency.

        Returns the AuthContext from AuthResolver, or None for generic hosts
        or when no dep_ref is provided.
        """
        if dep_ref is None:
            return None

        dep_host = dep_ref.host
        if self._is_generic_dependency_host(dep_ref):
            return None

        ctx = self.auth_resolver.resolve_for_dep(dep_ref)
        # Verbose source surfacing (#852): one-time per-host log line so users
        # can see which credential source was actually used. Routed through
        # AuthResolver.notify_auth_source() (#856 follow-up F2) so the line
        # obeys the same verbose-channel logic as every other diagnostic.
        if os.environ.get("APM_VERBOSE") == "1":
            self.auth_resolver.notify_auth_source(dep_host or "", ctx)
        return ctx
