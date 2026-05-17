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


class _GitEnvMixin:
    def _git_env_dict(self) -> dict[str, str]:
        """Return a sanitized git env dict for cache-layer subprocess calls.

        Delegates to :class:`GitAuthEnvBuilder.subprocess_env_dict`.
        """
        from ..git_auth_env import GitAuthEnvBuilder

        return GitAuthEnvBuilder.subprocess_env_dict(self.git_env)

    def _setup_git_environment(self) -> dict[str, Any]:
        """Set up Git environment with authentication using centralized token manager.

        Builds the auth-bearing env via :class:`GitAuthEnvBuilder`, then
        records token-state attributes on the downloader (these are read
        by many other methods on the class).
        """
        from ..git_auth_env import GitAuthEnvBuilder

        builder = GitAuthEnvBuilder(self.token_manager)
        env = builder.setup_environment()

        # IMPORTANT: Do not resolve credentials via helpers at construction time.
        # AuthResolver.resolve(...) can trigger OS credential helper UI. If we do
        # this eagerly (host-only key) and later resolve per-dependency (host+org),
        # users can see duplicate auth prompts. Keep constructor token state env-only
        # and resolve lazily per dependency during clone/validate flows.
        self.github_token = self.token_manager.get_token_for_purpose("modules", env)
        self.has_github_token = self.github_token is not None
        self._github_token_from_credential_fill = False

        # GitLab (env-only at init; lazy auth resolution happens per dep)
        self.gitlab_token = self.token_manager.get_token_for_purpose("gitlab_modules", env)
        self.has_gitlab_token = self.gitlab_token is not None

        # Azure DevOps (env-only at init; lazy auth resolution happens per dep)
        self.ado_token = self.token_manager.get_token_for_purpose("ado_modules", env)
        self.has_ado_token = self.ado_token is not None

        # JFrog Artifactory (not host-based, uses dedicated env var)
        self.artifactory_token = self.token_manager.get_token_for_purpose(
            "artifactory_modules", env
        )
        self.has_artifactory_token = self.artifactory_token is not None

        _debug(
            f"Token setup: has_github_token={self.has_github_token}, "
            f"has_gitlab_token={self.has_gitlab_token}, "
            f"has_ado_token={self.has_ado_token}, "
            f"has_artifactory_token={self.has_artifactory_token}"
            f"{', source=credential_helper' if self._github_token_from_credential_fill else ''}"
        )

        return env

    def _build_noninteractive_git_env(
        self,
        *,
        preserve_config_isolation: bool = False,
        suppress_credential_helpers: bool = False,
    ) -> dict[str, str]:
        """Return a non-interactive git env for unauthenticated git operations.

        Delegates to :class:`GitAuthEnvBuilder.noninteractive_env`.
        """
        from ..git_auth_env import GitAuthEnvBuilder

        return GitAuthEnvBuilder.noninteractive_env(
            self.git_env,
            preserve_config_isolation=preserve_config_isolation,
            suppress_credential_helpers=suppress_credential_helpers,
        )
