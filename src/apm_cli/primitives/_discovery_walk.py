"""Walk-and-match helpers for primitive file discovery.

Extracted from :mod:`discovery` to keep that module under 400 lines.
All public names continue to be importable from
:mod:`apm_cli.primitives.discovery` via explicit re-exports.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path

from ..constants import DEFAULT_SKIP_DIRS
from ..utils.exclude import should_exclude
from ..utils.paths import portable_relpath

logger = logging.getLogger(__name__)


def _glob_match(rel_path: str, pattern: str) -> bool:
    """Match a forward-slash relative path against a glob pattern.

    Segment-aware: ``*`` and ``?`` match within a single path segment only,
    while ``**`` matches zero or more complete segments. This preserves
    standard glob semantics so a pattern like
    ``**/.apm/instructions/*.instructions.md`` does not accidentally match
    ``.apm/instructions/sub/x.instructions.md`` (the trailing ``*`` must
    not cross ``/``).

    Args:
        rel_path: Relative path using forward slashes.
        pattern: Glob pattern using forward slashes.

    Returns:
        True if the path matches the pattern.
    """
    path_parts: list[str] = [p for p in rel_path.split("/") if p]
    pattern_parts: list[str] = [p for p in pattern.split("/") if p]
    memo: dict[tuple[int, int], bool] = {}

    def _match(pi: int, qi: int) -> bool:
        key = (pi, qi)
        if key in memo:
            return memo[key]

        if qi == len(pattern_parts):
            result = pi == len(path_parts)
            memo[key] = result
            return result

        current = pattern_parts[qi]

        if current == "**":
            # ** matches zero segments, OR consumes one segment and stays at **
            result = _match(pi, qi + 1)
            if not result and pi < len(path_parts):
                result = _match(pi + 1, qi)
            memo[key] = result
            return result

        if pi >= len(path_parts):
            memo[key] = False
            return False

        # Use platform-aware fnmatch semantics so Windows matching remains
        # case-insensitive, consistent with prior glob.glob() behavior.
        result = fnmatch.fnmatch(path_parts[pi], current) and _match(pi + 1, qi + 1)
        memo[key] = result
        return result

    return _match(0, 0)


def find_primitive_files(
    base_dir: str,
    patterns: list[str],
    exclude_patterns: list[str] | None = None,
) -> list[Path]:
    """Find primitive files matching the given patterns.

    Uses os.walk with early directory pruning instead of glob.glob(recursive=True)
    so that exclude_patterns prevent traversal into expensive subtrees.

    Symlinks are rejected outright to prevent symlink-based traversal
    attacks from malicious packages.

    Args:
        base_dir (str): Base directory to search in.
        patterns (List[str]): List of glob patterns to match.
        exclude_patterns (Optional[List[str]]): Pre-validated exclude patterns
            to prune directories early during traversal.

    Returns:
        List[Path]: List of file paths found.
    """
    if not os.path.isdir(base_dir):
        return []

    base_path = Path(base_dir).resolve()
    all_files = _walk_and_match_files(base_path, patterns, exclude_patterns)
    return _filter_valid_files(all_files)


def _walk_and_match_files(
    base_path: Path,
    patterns: list[str],
    exclude_patterns: list[str] | None,
) -> list[Path]:
    """Walk directory tree and collect files matching patterns."""
    all_files: list[Path] = []

    for root, dirs, files in os.walk(str(base_path)):
        current = Path(root)
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in DEFAULT_SKIP_DIRS
            and not _exclude_matches_dir(current / d, base_path, exclude_patterns)
        )

        for file_name in sorted(files):
            file_path = current / file_name
            rel_str = portable_relpath(file_path, base_path)
            if exclude_patterns and should_exclude(file_path, base_path, exclude_patterns):
                logger.debug("Excluded by pattern: %s", file_path)
                continue
            for pattern in patterns:
                if _glob_match(rel_str, pattern):
                    all_files.append(file_path)
                    break

    return all_files


def _filter_valid_files(files: list[Path]) -> list[Path]:
    """Filter out directories, symlinks, and unreadable files."""
    valid_files = []
    for file_path in files:
        if not file_path.is_file():
            continue
        if file_path.is_symlink():
            logger.debug("Rejected symlink: %s", file_path)
            continue
        if _is_readable(file_path):
            valid_files.append(file_path)
    return valid_files


def _exclude_matches_dir(
    dir_path: Path,
    base_path: Path,
    exclude_patterns: list[str] | None,
) -> bool:
    """Check if a directory matches any exclude pattern (for early pruning)."""
    if not exclude_patterns:
        return False
    return should_exclude(dir_path, base_path, exclude_patterns)


def _is_readable(file_path: Path) -> bool:
    """Check if a file is readable."""
    try:
        with open(file_path, encoding="utf-8") as f:
            # Try to read first few bytes to verify it's readable
            f.read(1)
        return True
    except (PermissionError, UnicodeDecodeError, OSError):
        return False


def _should_skip_directory(dir_path: str) -> bool:
    """Check if a directory should be skipped during scanning.

    Args:
        dir_path (str): Directory path to check.

    Returns:
        bool: True if directory should be skipped, False otherwise.
    """
    dir_name = os.path.basename(dir_path)
    return dir_name in DEFAULT_SKIP_DIRS
