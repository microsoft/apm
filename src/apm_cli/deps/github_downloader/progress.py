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


class GitProgressReporter(RemoteProgress):
    """Report git clone progress to Rich Progress."""

    def __init__(self, progress_task_id=None, progress_obj=None, package_name=None):
        super().__init__()
        self.task_id = progress_task_id
        self.progress = progress_obj
        self.package_name = package_name  # Keep consistent name throughout download
        self.last_op = None
        self.disabled = False  # Flag to stop updates after download completes

    def update(self, op_code, cur_count, max_count=None, message=""):
        """Called by GitPython during clone operations."""
        if not self.progress or self.task_id is None or self.disabled:
            return

        # Keep the package name consistent - don't change description to git operations
        # This keeps the UI clean and scannable

        # Update progress bar naturally - let it reach 100%
        if max_count and max_count > 0:
            # Determinate progress (we have total count)
            self.progress.update(
                self.task_id,
                completed=cur_count,
                total=max_count,
                # Note: We don't update description - keep the original package name
            )
        else:
            # Indeterminate progress (just show activity)
            self.progress.update(
                self.task_id,
                total=100,  # Set fake total for indeterminate tasks
                completed=min(cur_count, 100) if cur_count else 0,
                # Note: We don't update description - keep the original package name
            )

        self.last_op = cur_count

    def _get_op_name(self, op_code):
        """Convert git operation code to human-readable name."""
        from git import RemoteProgress

        # Extract operation type from op_code
        if op_code & RemoteProgress.COUNTING:
            return "Counting objects"
        elif op_code & RemoteProgress.COMPRESSING:
            return "Compressing objects"
        elif op_code & RemoteProgress.WRITING:
            return "Writing objects"
        elif op_code & RemoteProgress.RECEIVING:
            return "Receiving objects"
        elif op_code & RemoteProgress.RESOLVING:
            return "Resolving deltas"
        elif op_code & RemoteProgress.FINDING_SOURCES:
            return "Finding sources"
        elif op_code & RemoteProgress.CHECKING_OUT:
            return "Checking out files"
        else:
            return "Cloning"
