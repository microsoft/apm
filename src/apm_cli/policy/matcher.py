"""Pattern matching for policy allow/deny lists."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Protocol

from ..models.dependency.identity import normalize_package_policy_identity
from .schema import DependencyPolicy, McpPolicy


class _DependencyPolicySubject(Protocol):
    """Dependency identity fields required by policy matching."""

    @property
    def case_insensitive_identity_prefix_segments(self) -> int:
        """Return the repository-prefix length with presentation-only casing."""
        ...

    def get_canonical_dependency_string(self) -> str:
        """Return the dependency identity used by policy checks."""
        ...


class DependencyPolicyIndex:
    """Snapshot exact policy names once, retaining each source's casing rule.

    Buckets use the identity owner's prefix length, not a parallel host catalog.
    A match across buckets retains declaration order, as the original scan did.
    Legacy supplied name sets remain authoritative and byte-exact: they carry
    no source metadata, and an empty set must not trigger dependency discovery.
    """

    def __init__(self) -> None:
        self._names: dict[int, dict[str, tuple[int, str]]] = {}

    @classmethod
    def from_dependencies(cls, deps: Iterable[_DependencyPolicySubject]) -> DependencyPolicyIndex:
        """Project each dependency once into a source-aware lookup bucket."""
        index = cls()
        for position, dependency in enumerate(deps):
            name = dependency.get_canonical_dependency_string().split("#", 1)[0]
            prefix = dependency.case_insensitive_identity_prefix_segments
            normalized = normalize_package_policy_identity(
                name, case_insensitive_prefix_segments=prefix
            )
            index._names.setdefault(prefix, {}).setdefault(normalized, (position, name))
        return index

    @classmethod
    def from_names(cls, names: set[str]) -> DependencyPolicyIndex:
        """Snapshot the legacy authoritative name set without inspecting deps."""
        index = cls()
        index._names[0] = {name: (0, name) for name in names}
        return index

    def find_name(self, policy_name: str) -> str | None:
        """Return the first matching canonical name without re-projecting deps."""
        name = policy_name.split("#", 1)[0]
        first: tuple[int, str] | None = None
        for prefix, names in self._names.items():
            normalized = normalize_package_policy_identity(
                name, case_insensitive_prefix_segments=prefix
            )
            match = names.get(normalized)
            if match is not None and (first is None or match[0] < first[0]):
                first = match
        return first[1] if first is not None else None


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


def first_matching_pattern(name: str, patterns: tuple[str, ...] | None) -> str | None:
    """Return the first glob pattern in *patterns* that matches *name*.

    This case-sensitive path serves MCP and unmanaged-file checks. Dependency
    policy matching applies identity-aware operand normalization before routing
    through the same :func:`matches_pattern` glob implementation.
    """
    for pattern in patterns or ():
        if matches_pattern(name, pattern):
            return pattern
    return None


def _resolve_allow_deny(
    allow: tuple[str, ...] | None,
    deny: tuple[str, ...],
    first_match: Callable[[tuple[str, ...] | None], str | None],
) -> tuple[bool, str]:
    """Shared allow/deny logic.

    1. If ref matches any deny pattern -> denied.
    2. If allow is ``None`` -> allow (no opinion / deny-only mode).
    3. If allow is ``()`` -> block everything (explicit empty).
    4. If ref matches any allow pattern -> allowed.
    5. Otherwise -> not in allowed sources.
    """
    denied = first_match(deny)
    if denied is not None:
        return False, f"denied by pattern: {denied}"

    if allow is None:
        return True, ""

    if first_match(allow) is not None:
        return True, ""

    return False, "not in allowed sources"


def _first_matching_dependency_pattern(
    dependency: _DependencyPolicySubject,
    patterns: tuple[str, ...] | None,
) -> str | None:
    """Return the first pattern matching one dependency's identity semantics."""
    prefix_segments = dependency.case_insensitive_identity_prefix_segments
    canonical_ref = normalize_package_policy_identity(
        dependency.get_canonical_dependency_string(),
        case_insensitive_prefix_segments=prefix_segments,
    )
    for pattern in patterns or ():
        normalized_pattern = normalize_package_policy_identity(
            pattern,
            case_insensitive_prefix_segments=prefix_segments,
        )
        if matches_pattern(canonical_ref, normalized_pattern):
            return pattern
    return None


def check_dependency_allowed(
    dependency: _DependencyPolicySubject | str,
    policy: DependencyPolicy,
) -> tuple[bool, str]:
    """Check if a dependency is allowed by policy.

    Raw canonical strings retain the exported API's case-sensitive behavior.
    Policy enforcement passes a dependency object so its canonical identity
    authority controls operand normalization.
    """
    if isinstance(dependency, str):
        return _resolve_allow_deny(
            policy.allow,
            policy.effective_deny,
            lambda patterns: first_matching_pattern(dependency, patterns),
        )
    return _resolve_allow_deny(
        policy.allow,
        policy.effective_deny,
        lambda patterns: _first_matching_dependency_pattern(dependency, patterns),
    )


def dependency_policy_name_matches(
    dependency: _DependencyPolicySubject,
    policy_name: str,
) -> bool:
    """Return whether an exact policy package name identifies *dependency*."""
    prefix_segments = dependency.case_insensitive_identity_prefix_segments
    canonical_name = dependency.get_canonical_dependency_string().split("#", 1)[0]
    expected_name = policy_name.split("#", 1)[0]
    return normalize_package_policy_identity(
        canonical_name,
        case_insensitive_prefix_segments=prefix_segments,
    ) == normalize_package_policy_identity(
        expected_name,
        case_insensitive_prefix_segments=prefix_segments,
    )


def check_mcp_allowed(
    server_name: str,
    policy: McpPolicy,
) -> tuple[bool, str]:
    """Check if an MCP server is allowed by policy."""
    return _resolve_allow_deny(
        policy.allow,
        policy.deny,
        lambda patterns: first_matching_pattern(server_name, patterns),
    )
