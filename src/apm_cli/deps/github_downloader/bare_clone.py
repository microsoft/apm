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


class _BareCloneMixin:
    def _bare_clone_with_fallback(
        self,
        repo_url_base: str,
        bare_target: Path,
        *,
        dep_ref: DependencyReference,
        ref: str | None,
        is_commit_sha: bool,
    ) -> None:
        """Thin delegate to :func:`bare_cache.bare_clone_with_fallback` (kept on the class so test patches still work)."""
        bare_clone_with_fallback(
            self._execute_transport_plan,
            repo_url_base,
            bare_target,
            dep_ref=dep_ref,
            ref=ref,
            is_commit_sha=is_commit_sha,
        )

    def _materialize_from_bare(
        self,
        bare_path: Path,
        consumer_dir: Path,
        *,
        ref: str | None,
        env: dict[str, str],
        known_sha: str | None = None,
    ) -> str:
        """Thin delegate to :func:`bare_cache.materialize_from_bare` (kept on the class so test patches still work)."""
        return materialize_from_bare(bare_path, consumer_dir, ref=ref, env=env, known_sha=known_sha)

    def _fetch_sha_into_bare(
        self,
        bare_path: Path,
        sha: str,
        *,
        dep_ref: "DependencyReference",
    ) -> bool:
        """Thin delegate to :func:`bare_cache.fetch_sha_into_bare` (kept on the class so test patches still work)."""
        return fetch_sha_into_bare(
            self._execute_transport_plan,
            dep_ref.repo_url,
            bare_path,
            sha,
            dep_ref=dep_ref,
        )
