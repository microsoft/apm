"""Pattern matching for policy allow/deny lists."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

from ..models.dependency.identity import (
    effective_policy_host,
    normalize_package_policy_identity,
    required_package_policy_hosts,
    split_policy_pattern_host,
)
from .schema import DependencyPolicy, McpPolicy

if TYPE_CHECKING:
    from ..deps.lockfile import LockedDependency, LockFile


class _DependencyPolicySubject(Protocol):
    """Dependency identity fields required by policy matching."""

    @property
    def case_insensitive_identity_prefix_segments(self) -> int:
        """Return the repository-prefix length with presentation-only casing."""
        ...

    @property
    def policy_host(self) -> str | None:
        """Return the effective host for host-qualified patterns, or ``None``."""
        ...

    def get_canonical_dependency_string(self) -> str:
        """Return the dependency identity used by policy checks."""
        ...

    def get_unique_key(self) -> str:
        """Return the lockfile key of this dependency."""
        ...


@dataclass(frozen=True)
class RequiredPackageMatch:
    """One declared dependency an exact ``require`` entry identifies."""

    position: int
    name: str
    host: str | None
    dependency: _DependencyPolicySubject | None = None

    @property
    def lock_key(self) -> str:
        """Return the lockfile key, derived by the lockfile's own key owner.

        A legacy supplied name carries no source metadata; its byte-exact name
        stays the key, as before.
        """
        if self.dependency is None:
            return self.name
        return self.dependency.get_unique_key()

    def locked_in(self, lock: LockFile) -> LockedDependency | None:
        """Return this dependency's lockfile row -- the only required-check lock read."""
        if self.dependency is None:
            return lock.get_dependency(self.lock_key)
        from ..deps.lockfile_lookup import locked_row_for

        return locked_row_for(lock, self.dependency)

    @property
    def require_entry(self) -> str:
        """Return the ``require`` spelling that identifies this dependency."""
        if self.host in (None, effective_policy_host(None)):
            return self.name
        return f"{self.host}/{self.name}"


class DependencyPolicyIndex:
    """Snapshot exact policy names once, retaining each source's casing rule.

    Buckets use the identity owner's prefix length, not a parallel host catalog.
    Exact ``require`` entries are host-aware: a hostless entry names the
    default host and a host-qualified entry names that host, on the manifest
    and the lockfile side alike. A match across buckets retains declaration
    order, as the original scan did. Legacy supplied name sets remain
    authoritative and byte-exact: they carry no source metadata, and an empty
    set must not trigger dependency discovery.
    """

    def __init__(self) -> None:
        self._names: dict[int, dict[str, list[RequiredPackageMatch]]] = {}
        self._legacy = False

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
            index._names.setdefault(prefix, {}).setdefault(normalized, []).append(
                RequiredPackageMatch(position, name, dependency.policy_host, dependency)
            )
        return index

    @classmethod
    def from_names(cls, names: set[str]) -> DependencyPolicyIndex:
        """Snapshot the legacy authoritative name set without inspecting deps."""
        index = cls()
        index._legacy = True
        index._names[0] = {name: [RequiredPackageMatch(0, name, None)] for name in names}
        return index

    def find(self, policy_name: str) -> RequiredPackageMatch | None:
        """Return the first dependency *policy_name* identifies, host included."""
        if self._legacy:
            hosts, path = None, policy_name.split("#", 1)[0]
        else:
            hosts, path = required_package_policy_hosts(policy_name)
        return self._first(path, hosts)

    def find_name(self, policy_name: str) -> str | None:
        """Return the first matching canonical name without re-projecting deps."""
        match = self.find(policy_name)
        return match.name if match is not None else None

    def find_or_undeclared(self, policy_name: str) -> RequiredPackageMatch | None:
        """Return the declared match, else a byte-exact subject for an undeclared name.

        The undeclared subject keeps the legacy raw lock lookup for a package
        declared on no host at all; a same-path package declared on another
        host returns ``None``, so its lock row is never read for this entry.
        """
        match = self.find(policy_name)
        if match is not None or self.host_mismatch(policy_name) is not None:
            return match
        return RequiredPackageMatch(0, policy_name.split("#", 1)[0], None)

    def host_mismatch(self, policy_name: str) -> tuple[str | None, RequiredPackageMatch] | None:
        """Return ``(entry_host, declared)`` when the entry's path is declared on another host.

        ``entry_host`` is the host the entry names (the default host when
        hostless); ``declared`` is the same-path dependency it did not match.
        """
        if self._legacy:
            return None
        hosts, path = required_package_policy_hosts(policy_name)
        declared = self._first(path, None)
        return (hosts[0], declared) if declared is not None else None

    def _first(
        self, path: str, hosts: tuple[str | None, ...] | None
    ) -> RequiredPackageMatch | None:
        first: RequiredPackageMatch | None = None
        for prefix, names in self._names.items():
            normalized = normalize_package_policy_identity(
                path, case_insensitive_prefix_segments=prefix
            )
            match = next(
                (m for m in names.get(normalized, ()) if hosts is None or m.host in hosts),
                None,
            )
            if match is not None and (first is None or match.position < first.position):
                first = match
        return first


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
    """Return the first pattern matching one dependency's identity semantics.

    A host-qualified pattern applies only when its host equals the
    dependency's effective host; its remaining path is matched like a hostless
    pattern. Hostless patterns match the dependency on any host.
    """
    prefix_segments = dependency.case_insensitive_identity_prefix_segments
    canonical_ref = normalize_package_policy_identity(
        dependency.get_canonical_dependency_string(),
        case_insensitive_prefix_segments=prefix_segments,
    )
    dependency_host = dependency.policy_host
    for pattern in patterns or ():
        pattern_host, path_pattern = split_policy_pattern_host(pattern)
        if pattern_host is not None and pattern_host != dependency_host:
            continue
        normalized_pattern = normalize_package_policy_identity(
            path_pattern,
            case_insensitive_prefix_segments=prefix_segments,
        )
        if matches_pattern(canonical_ref, normalized_pattern):
            return pattern
    return None


def dependency_policy_subject(dependency: _DependencyPolicySubject) -> str:
    """Return the host-qualified identity that dependency diagnostics display."""
    canonical_ref = dependency.get_canonical_dependency_string()
    host = dependency.policy_host
    return f"{host}/{canonical_ref}" if host else canonical_ref


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
    """Return whether an exact policy ``require`` entry identifies *dependency*."""
    return DependencyPolicyIndex.from_dependencies((dependency,)).find(policy_name) is not None


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
