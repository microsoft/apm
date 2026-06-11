"""Git-transport-first single-file fetcher for path:-specifier deps (issue #1014).

When a dependency's source is already a git/SSH repo, this module
extracts path:-specified files through a sparse/partial git checkout
(blob:none + sparse paths) rather than calling the host REST API. This
fixes self-hosted GitLab instances where the API returns 410 (disabled).

Design constraints
------------------
* git-transport-first: git is tried before the REST API for GitLab and
  generic git sources.
* No new credentials: SSH keys and system git credential fill are used;
  the function inherits the same auth environment as regular clones.
* ensure_path_within() is applied to the materialized path before
  reading it, preventing traversal and symlink-escape attacks.
* Same-run batching: callers can reuse one sparse/partial checkout for
  multiple files from the same repo/ref. The temp directory is cleaned
  up when the transport closes.
* File-level sparse-checkout uses non-cone sparse paths (git 2.25+) so
  root-level files do not trigger whole-tree materialization.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..utils.path_security import ensure_path_within, validate_path_segments

if TYPE_CHECKING:
    from ..models.apm_package import DependencyReference

_GIT_TIMEOUT = 120  # seconds per git subprocess


class GitFileTransportError(RuntimeError):
    """Transport-level git failure during path-scoped file fetch."""


def _debug(message: str) -> None:
    """Print debug message if APM_DEBUG environment variable is set."""
    if os.environ.get("APM_DEBUG"):
        print(f"[DEBUG] {message}", file=sys.stderr)


def _redact_git_stderr(stderr: str) -> str:
    """Redact auth-bearing HTTPS URL credentials from git stderr."""
    cleaned = stderr.strip()
    return re.sub(r"(https://)[^/@\s]+@", r"\1***@", cleaned)


class GitSparseFileTransport:
    """Reusable sparse/partial checkout for path-scoped git file fetches."""

    def __init__(
        self,
        dep_ref: DependencyReference,
        ref: str,
        *,
        build_repo_url_fn: Callable[..., str],
        git_env: dict[str, str],
        timeout: int = _GIT_TIMEOUT,
    ) -> None:
        """Create a reusable sparse checkout for one repository and ref."""
        self._dep_ref = dep_ref
        self._ref = ref
        self._build_repo_url_fn = build_repo_url_fn
        self._git_env = {**git_env, "GIT_TERMINAL_PROMPT": "0"}
        self._timeout = timeout
        self._temp_dir = tempfile.TemporaryDirectory(prefix="apm_gitfetch_")
        self._work_dir = Path(self._temp_dir.name) / "work"
        self._auth_url: str | None = None
        self._sparse_paths: set[str] = set()
        self._initialized = False
        self._lock = threading.Lock()
        self._state = threading.Condition()
        self._active_fetches = 0
        self._closed = False

    def __enter__(self) -> GitSparseFileTransport:
        """Return this transport as a context manager value."""
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Clean up the temporary checkout when the context exits."""
        self.close()

    def close(self) -> None:
        """Remove the temporary checkout backing this transport."""
        with self._state:
            if self._closed:
                return
            self._closed = True
            while self._active_fetches:
                self._state.wait()
        with self._lock:
            self._temp_dir.cleanup()

    def fetch_file(self, file_path: str) -> bytes:
        """Fetch one file from the transport's repository/ref."""
        with self._state:
            if self._closed:
                raise GitFileTransportError("git file transport is closed")
            self._active_fetches += 1
        try:
            with self._lock:
                validate_path_segments(file_path, context="path")
                self._ensure_checkout(file_path)

                target = self._work_dir / file_path
                ensure_path_within(target, self._work_dir)

                if not target.exists():
                    raise RuntimeError(
                        f"File '{file_path}' not found after git sparse checkout of "
                        f"{self._dep_ref.host}/{self._dep_ref.repo_url}@{self._ref}"
                    )

                return target.read_bytes()
        finally:
            with self._state:
                self._active_fetches -= 1
                if self._active_fetches == 0:
                    self._state.notify_all()

    def _ensure_checkout(self, file_path: str) -> None:
        """Initialize or expand the sparse checkout for file_path."""
        self._work_dir.mkdir(exist_ok=True)
        if not self._initialized:
            self._auth_url = self._build_repo_url_fn(
                self._dep_ref.repo_url,
                dep_ref=self._dep_ref,
            )
            self._run(["git", "init"])
            self._run(["git", "remote", "add", "origin", self._auth_url])
            self._run(["git", "sparse-checkout", "init", "--no-cone"])
            self._set_sparse_paths(file_path)
            _debug(
                "git sparse fetch: "
                f"host={self._dep_ref.host} repo={self._dep_ref.repo_url} "
                f"ref={self._ref} paths={len(self._sparse_paths)}"
            )
            self._run(["git", "fetch", "--filter=blob:none", "--depth=1", "origin", self._ref])
            self._run(["git", "checkout", "FETCH_HEAD"])
            self._initialized = True
            return

        if file_path not in self._sparse_paths:
            self._set_sparse_paths(file_path)
            self._run(["git", "checkout", "FETCH_HEAD"])

    def _set_sparse_paths(self, file_path: str) -> None:
        """Apply the accumulated file-level sparse paths."""
        self._sparse_paths.add(file_path)
        self._run(["git", "sparse-checkout", "set", "--no-cone", *sorted(self._sparse_paths)])

    def _run(self, cmd: list[str]) -> None:
        """Run one git command and raise a sanitized error on failure."""
        result = subprocess.run(
            cmd,
            cwd=str(self._work_dir),
            env=self._git_env,
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        if result.returncode != 0:
            safe_stderr = _redact_git_stderr(result.stderr)
            raise GitFileTransportError(
                f"git file fetch failed: {' '.join(cmd[:3])}: {safe_stderr}"
            )


def fetch_file_via_git_sparse(
    dep_ref: DependencyReference,
    file_path: str,
    ref: str,
    *,
    build_repo_url_fn: Callable[..., str],
    git_env: dict[str, str],
    timeout: int = _GIT_TIMEOUT,
) -> bytes:
    """Fetch a single file from a git repo via sparse/partial checkout.

    Performs a depth-1, blob:none sparse checkout to extract only the
    requested file without downloading the full repository. Applies
    ensure_path_within() containment on the materialized path to reject
    symlink/traversal escapes from a cloned repository.

    Args:
        dep_ref: Parsed dependency reference (host, repo_url, etc.).
        file_path: Path to the file within the repository (e.g.
            ``"agents/api-specialist.agent.md"``).
        ref: Git ref (branch, tag, or commit SHA).
        build_repo_url_fn: Callable that returns an auth-embedded clone
            URL for dep_ref. Injected to avoid circular imports with the
            owning downloader.
        git_env: Subprocess environment dict (inherits git auth, e.g.
            GIT_ASKPASS, GH_TOKEN, SSH agent forwarding).
        timeout: Per-subprocess timeout in seconds.

    Returns:
        bytes: Raw file content.

    Raises:
        PathTraversalError: If file_path contains traversal segments
            (``..``) or the checked-out file is a symlink that escapes
            the temporary work tree.
        RuntimeError: If any git command fails or the file is absent
            after checkout.
    """
    with GitSparseFileTransport(
        dep_ref,
        ref,
        build_repo_url_fn=build_repo_url_fn,
        git_env=git_env,
        timeout=timeout,
    ) as transport:
        return transport.fetch_file(file_path)
