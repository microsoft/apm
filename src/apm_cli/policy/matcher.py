"""Pattern matching for policy allow/deny lists."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional, Tuple  # noqa: F401, UP035

from .schema import DependencyPolicy, McpPolicy


@lru_cache(maxsize=512)
def _compile_pattern(pattern: str) -> re.Pattern:
    """Compile a policy glob pattern into a regex.

    - ``*`` matches within a single segment (no ``/``).
    - ``**`` matches any depth (zero or more segments including ``/``).
    - Everything else is matched literally.
    """
    parts = re.split(r"(\*\*|\*)", pattern)
    regex = ""
    for part in parts:
        if part == "**":
            regex += ".*"
        elif part == "*":
            regex += "[^/]*"
        else:
            regex += re.escape(part)
    return re.compile(f"^{regex}$")


def matches_pattern(canonical_ref: str, pattern: str) -> bool:
    """Check if a canonical dependency ref matches a policy pattern."""
    if not pattern or not canonical_ref:
        return False

    # Fast path: exact match
    if canonical_ref == pattern:
        return True

    return bool(_compile_pattern(pattern).match(canonical_ref))


def _check_allow_deny(
    ref: str,
    allow: tuple[str, ...] | None,
    deny: tuple[str, ...],
) -> tuple[bool, str]:
    """Shared allow/deny logic.

    1. If ref matches any deny pattern -> denied.
    2. If allow is ``None`` -> allow (no opinion / deny-only mode).
    3. If allow is ``()`` -> block everything (explicit empty).
    4. If ref matches any allow pattern -> allowed.
    5. Otherwise -> not in allowed sources.
    """
    for pattern in deny:
        if matches_pattern(ref, pattern):
            return False, f"denied by pattern: {pattern}"

    if allow is None:
        return True, ""

    for pattern in allow:
        if matches_pattern(ref, pattern):
            return True, ""

    return False, "not in allowed sources"


def check_dependency_allowed(
    canonical_ref: str,
    policy: DependencyPolicy,
) -> tuple[bool, str]:
    """Check if a dependency is allowed by policy."""
    return _check_allow_deny(canonical_ref, policy.allow, policy.deny)


def check_mcp_allowed(
    server_name: str,
    policy: McpPolicy,
) -> tuple[bool, str]:
    """Check if an MCP server is allowed by policy."""
    return _check_allow_deny(server_name, policy.allow, policy.deny)
