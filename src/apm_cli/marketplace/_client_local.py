"""Local filesystem fetcher helpers for the marketplace client.

Contains the sub-functions of ``_fetch_local`` split out to keep
``client.py`` under the 800-line budget.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import MarketplaceSource


def _fetch_local_file(source: MarketplaceSource, manifest_file: Path) -> dict | None:
    """Read an explicit local marketplace.json file.

    The parent directory is the containment boundary by design: unlike a
    directory source, a direct file source is a single user-selected file, so
    there is no broader marketplace root to enforce.
    """
    from ..utils.path_security import PathTraversalError, ensure_path_within
    from .errors import MarketplaceFetchError

    try:
        safe_file = ensure_path_within(manifest_file, manifest_file.parent)
    except PathTraversalError as exc:
        raise MarketplaceFetchError(
            source.name, "local marketplace file escapes its parent"
        ) from exc

    try:
        with open(safe_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise MarketplaceFetchError(source.name, f"failed to read {safe_file}: {exc}") from exc


def _fetch_local_via_git_show(
    source: MarketplaceSource, file_path: str, git_dir: Path
) -> dict | None:
    """Use ``git show <ref>:<file>`` against a bare repo or .git directory."""
    from ..utils.git_env import git_subprocess_env
    from .errors import MarketplaceFetchError

    cmd = [
        "git",
        "--git-dir",
        str(git_dir),
        "-c",
        "core.hooksPath=/dev/null",
        "show",
        f"{source.ref}:{file_path}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=30,
            env=git_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise MarketplaceFetchError(source.name, f"git show failed for {file_path}: {exc}") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if (
            "does not exist" in stderr.lower()
            or "exists on disk, but not in" in stderr.lower()
            or "fatal: path" in stderr.lower()
        ):
            return None
        raise MarketplaceFetchError(source.name, f"git show failed: {stderr}")

    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MarketplaceFetchError(source.name, f"invalid JSON in {file_path}: {exc}") from exc


def _fetch_local_direct_read(
    source: MarketplaceSource, file_path: str, repo_root: Path
) -> dict | None:
    """Read a file directly from a working-dir local marketplace."""
    from ..utils.path_security import PathTraversalError, ensure_path_within
    from .errors import MarketplaceFetchError

    candidate = (repo_root / file_path).resolve(strict=False)
    try:
        ensure_path_within(candidate, repo_root)
    except PathTraversalError as exc:
        raise MarketplaceFetchError(
            source.name, f"path escapes marketplace root: {file_path}"
        ) from exc

    if not candidate.exists():
        return None
    try:
        with open(candidate, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError, RecursionError, MemoryError) as exc:
        raise MarketplaceFetchError(source.name, f"failed to read {file_path}: {exc}") from exc
