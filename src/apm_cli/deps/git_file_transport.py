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
* No clone caching: depth-1 + filter=blob:none makes the fetch cheap
  enough that caching is not worth the added complexity for a single
  file. The temp directory is cleaned up on exit.
* Cone-mode sparse-checkout is used for subdirectory files (git 2.25+).
  Root-level files skip cone setup and rely on the blob filter alone.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..utils.path_security import ensure_path_within, validate_path_segments

if TYPE_CHECKING:
    from ..models.apm_package import DependencyReference

_GIT_TIMEOUT = 120  # seconds per git subprocess


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

    Performs a depth-1, blob:none sparse clone to extract only the
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
    # Reject traversal sequences in the path string *before* any git work.
    validate_path_segments(file_path, context="path")

    auth_url = build_repo_url_fn(dep_ref.repo_url, dep_ref=dep_ref)

    tmp_dir = tempfile.mkdtemp(prefix="apm_gitfetch_")
    try:
        work_dir = Path(tmp_dir) / "work"
        work_dir.mkdir(exist_ok=True)

        # Cone-mode sparse-checkout needs a *directory* path, not a file.
        # Determine the parent: for "a/b/c.md" the cone is "a/b".
        # For root-level files ("c.md"), skip cone -- depth=1 + blob:none
        # keeps the fetch lightweight without needing cone setup.
        file_parent = str(Path(file_path).parent)
        use_sparse_cone = file_parent != "."

        cmds: list[list[str]] = [
            ["git", "init"],
            ["git", "remote", "add", "origin", auth_url],
        ]
        if use_sparse_cone:
            cmds += [
                ["git", "sparse-checkout", "init", "--cone"],
                ["git", "sparse-checkout", "set", file_parent],
            ]
        # --filter=blob:none: trees + commits are fetched eagerly; blobs are
        # fetched on demand during checkout.  Combined with sparse-checkout,
        # only the blobs in the requested directory are materialized.
        cmds += [
            ["git", "fetch", "--filter=blob:none", "--depth=1", "origin", ref],
            ["git", "checkout", "FETCH_HEAD"],
        ]

        for cmd in cmds:
            result = subprocess.run(
                cmd,
                cwd=str(work_dir),
                env=git_env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"git file fetch failed: {' '.join(cmd[:3])}: {result.stderr.strip()}"
                )

        target = work_dir / file_path
        # Containment check: resolves symlinks so a link pointing outside
        # the checkout is detected and rejected.
        ensure_path_within(target, work_dir)

        if not target.exists():
            raise RuntimeError(
                f"File '{file_path}' not found after git sparse checkout of "
                f"{dep_ref.host}/{dep_ref.repo_url}@{ref}"
            )

        return target.read_bytes()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
