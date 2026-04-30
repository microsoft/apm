"""Shared exclude-pattern matching for compilation and primitive discovery.

Provides glob-style pattern matching with ** (recursive directory) support.
Used by both the context optimizer and primitive discovery to filter paths
against compilation.exclude patterns from apm.yml.
"""

import fnmatch
import logging
import os
from pathlib import Path
from typing import List, Optional  # noqa: F401, UP035

logger = logging.getLogger(__name__)

# Maximum number of ** segments allowed in a single pattern.
# Prevents exponential recursion blowup (2^N branches per ** segment).
_MAX_DOUBLE_STAR_SEGMENTS = 5


def validate_exclude_patterns(patterns: list[str] | None) -> list[str]:
    """Validate and normalize exclude patterns, rejecting dangerous ones.

    Args:
        patterns: Raw patterns from apm.yml compilation.exclude.

    Returns:
        List of validated, forward-slash-normalized patterns.

    Raises:
        ValueError: If a pattern exceeds the ** segment safety limit.
    """
    if not patterns:
        return []

    validated = []
    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        # Collapse consecutive ** segments (semantically identical to single **)
        parts = normalized.split("/")
        collapsed = []
        for p in parts:
            if p == "**" and collapsed and collapsed[-1] == "**":
                continue
            collapsed.append(p)
        normalized = "/".join(collapsed)
        star_count = collapsed.count("**")
        if star_count > _MAX_DOUBLE_STAR_SEGMENTS:
            raise ValueError(
                f"Exclude pattern '{pattern}' has {star_count} '**' segments "
                f"(max {_MAX_DOUBLE_STAR_SEGMENTS}). Simplify the pattern."
            )
        validated.append(normalized)
    return validated


def should_exclude(
    file_path: Path,
    base_dir: Path,
    exclude_patterns: list[str] | None,
) -> bool:
    """Check whether a file path should be excluded.

    Args:
        file_path: Absolute or relative path of the discovered file.
        base_dir: Project base directory for computing relative paths.
        exclude_patterns: Pre-validated, forward-slash-normalized patterns.

    Returns:
        True if the file matches any exclusion pattern.
    """
    if not exclude_patterns:
        return False

    try:
        resolved = file_path.resolve()
    except (OSError, FileNotFoundError):
        resolved = file_path.absolute()
    try:
        rel_path = resolved.relative_to(base_dir.resolve())
    except ValueError:
        return False

    rel_path_str = str(rel_path).replace(os.sep, "/")

    for pattern in exclude_patterns:  # noqa: SIM110
        if _matches_pattern(rel_path_str, pattern):
            return True

    return False


def _matches_pattern(rel_path_str: str, pattern: str) -> bool:
    """Check if a relative path string matches a single exclusion pattern.

    Supports glob wildcards including ** for recursive directory matching.
    """
    if "**" in pattern:
        path_parts = rel_path_str.split("/")
        pattern_parts = pattern.split("/")
        return _match_glob_recursive(path_parts, pattern_parts)

    if fnmatch.fnmatch(rel_path_str, pattern):
        return True

    # Directory prefix matching: "docs/" or "docs" should match "docs/foo.md"
    if pattern.endswith("/"):
        if rel_path_str.startswith(pattern) or rel_path_str == pattern.rstrip("/"):
            return True
    elif rel_path_str.startswith(pattern + "/") or rel_path_str == pattern:
        return True

    return False


def _match_glob_recursive(path_parts: list, pattern_parts: list) -> bool:
    """Match path components against pattern components with ** support.

    Uses iterative consumption for consecutive ** segments to avoid
    exponential branching, then falls back to bounded recursion for
    mixed patterns.
    """
    # Strip leading empty parts from trailing slashes in patterns
    while pattern_parts and pattern_parts[-1] == "":
        pattern_parts = pattern_parts[:-1]

    pi = 0  # pattern index
    xi = 0  # path index

    # Fast iterative path for leading non-** segments
    while pi < len(pattern_parts) and xi < len(path_parts):
        part = pattern_parts[pi]
        if part == "**":
            break
        if fnmatch.fnmatch(path_parts[xi], part):
            pi += 1
            xi += 1
        else:
            return False

    # If no ** was encountered, both must be exhausted
    if pi == len(pattern_parts):
        return xi == len(path_parts)

    # Delegate remaining ** matching via bounded recursion
    return _match_double_star(path_parts[xi:], pattern_parts[pi:])


def _match_double_star(path_parts: list, pattern_parts: list) -> bool:
    """Handle ** segments with bounded recursion."""
    if not pattern_parts:
        return not path_parts

    if not path_parts:
        return all(p == "**" or p == "" for p in pattern_parts)  # noqa: PLR1714

    part = pattern_parts[0]

    if part == "**":
        # ** matches zero or more directories
        if _match_double_star(path_parts, pattern_parts[1:]):
            return True
        if _match_double_star(path_parts[1:], pattern_parts):  # noqa: SIM103
            return True
        return False
    else:
        if fnmatch.fnmatch(path_parts[0], part):
            return _match_double_star(path_parts[1:], pattern_parts[1:])
        return False
