"""Commit-based admission of immutable edges before single-version hoisting."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import (
    GitReferenceType,
    RemoteRef,
    ResolvedReference,
    parse_git_reference,
)

from .dependency_graph import DependencyNode
from .lockfile import LockFile
from .revision_pins import is_full_revision_pin


class ReferenceResolver(Protocol):
    """Existing downloader operations needed to prove ref equivalence."""

    def list_remote_refs(self, dep_ref: DependencyReference) -> Iterable[RemoteRef]: ...

    def resolve_git_reference(self, repo_ref: DependencyReference) -> ResolvedReference: ...


class ImmutableRequirementError(ValueError):
    """An immutable requirement cannot be satisfied or verified."""


@dataclass(frozen=True)
class _Requirement:
    dependency: DependencyReference
    chain: str


class ImmutableRequirements:
    """Preserve immutable obligations independently of materialization winners.

    Ordinary, repeated requirements need no remote calls. Distinct literal
    refs are compared by commit, with one remote listing per repository.
    Branches and ranges retain their existing resolution policy.
    """

    def __init__(
        self,
        root_name: str,
        resolver: ReferenceResolver | None = None,
        lockfile: LockFile | None = None,
        *,
        frozen: bool = False,
        update_refs: bool = False,
    ) -> None:
        self._root_name = root_name
        self._resolver = resolver
        self._lockfile = lockfile
        self._frozen = frozen
        self._update_refs = update_refs
        self._first: dict[str, _Requirement] = {}
        self._commits: dict[str, str | None] = {}
        self._remote_refs: dict[str, dict[tuple[str, GitReferenceType], list[RemoteRef]]] = {}

    def add(self, node: DependencyNode) -> None:
        """Validate one declared edge before its ref or materialization changes."""
        dep = node.dependency_ref
        if (
            dep.is_local
            or dep.source == "registry"
            or dep.artifactory_prefix
            or not dep.reference
            or dep.ref_kind == "semver"
        ):
            return
        parts = []
        current: DependencyNode | None = node
        while current is not None:
            edge = current.dependency_ref
            parts.append(f"{edge.get_unique_key()}@{edge.reference or 'HEAD'}")
            current = current.parent
        requirement = _Requirement(replace(dep), " -> ".join([self._root_name, *reversed(parts)]))
        key = dep.get_unique_key()
        first = self._first.setdefault(key, requirement)
        if first.dependency.reference != dep.reference:
            first_commit = self._commit(first, requirement.chain)
            commit = self._commit(requirement, first.chain)
            if first_commit is not None and commit is not None:
                self._require_equal(first.chain, first_commit, requirement.chain, commit, key)
            elif first_commit is None and commit is not None:
                self._first[key] = requirement
        if self._frozen:
            self._check_locked(requirement)

    def _require_equal(
        self, first_chain: str, first_commit: str, chain: str, commit: str, key: str
    ) -> None:
        if first_commit != commit:
            mode_hint = " without --frozen" if self._frozen else ""
            raise ImmutableRequirementError(
                f"Cannot resolve {key}: incompatible immutable requirements.\n"
                f"  {first_chain} (commit {first_commit})\n"
                f"  {chain} (commit {commit})\n"
                "Align the requested refs in the root or parent manifests to the same "
                f"commit, then run 'apm install'{mode_hint} to regenerate apm.lock.yaml."
            )

    def _check_locked(self, requirement: _Requirement) -> None:
        dep = requirement.dependency
        locked = self._lockfile.get_dependency(dep.get_unique_key()) if self._lockfile else None
        if locked is None:
            raise ImmutableRequirementError(
                f"--frozen: {requirement.chain} is missing from apm.lock.yaml. "
                "Run 'apm install' without --frozen to regenerate the lockfile."
            )
        locked_commit = locked.resolved_commit
        if not is_full_revision_pin(locked_commit):
            raise ImmutableRequirementError(
                f"--frozen: apm.lock.yaml has no verifiable commit for {requirement.chain}. "
                "Run 'apm install' without --frozen to regenerate the lockfile."
            )
        ref_type, ref = parse_git_reference(dep.reference)
        locked_chain = f"apm.lock.yaml -> {dep.get_unique_key()}@{locked.resolved_ref or 'HEAD'}"
        if ref_type == GitReferenceType.COMMIT and not locked_commit.lower().startswith(
            ref.lower()
        ):
            self._require_equal(
                locked_chain,
                locked_commit.lower(),
                requirement.chain,
                ref.lower(),
                dep.get_unique_key(),
            )
        if dep.reference == locked.resolved_ref:
            return
        commit = self._commit(requirement)
        if commit is None:
            return
        self._require_equal(
            locked_chain,
            locked_commit.lower(),
            requirement.chain,
            commit,
            dep.get_unique_key(),
        )

    def _commit(self, requirement: _Requirement, other_chain: str | None = None) -> str | None:
        """Return a proven immutable commit, or None for a mutable Git ref."""
        dep = requirement.dependency
        key = dep.get_resolution_key()
        if key in self._commits:
            return self._commits[key]
        if is_full_revision_pin(dep.reference):
            commit = dep.reference.lower()
        else:
            try:
                commit = self._resolve_commit(dep)
            except (OSError, RuntimeError, ValueError) as exc:
                chains = requirement.chain
                if other_chain is not None:
                    chains += f"\n  {other_chain}"
                raise ImmutableRequirementError(
                    f"Cannot verify immutable requirements:\n  {chains}\n"
                    "Check that the requested refs exist and the repository is accessible, "
                    "then retry. For private repositories, check the authentication guide. "
                    "Different ref names alone do not prove a conflict."
                ) from exc
        self._commits[key] = commit
        return commit

    def _resolve_commit(self, dep: DependencyReference) -> str | None:
        ref_type, ref = parse_git_reference(dep.reference)
        locked = self._lockfile.get_dependency(dep.get_unique_key()) if self._lockfile else None
        if not self._update_refs and locked and locked.resolved_ref == dep.reference:
            # Replay anchors content even for a named branch; do not rediscover it.
            commit = locked.resolved_commit
            if not is_full_revision_pin(commit):
                raise ValueError("Locked reference has no full commit")
            if ref_type == GitReferenceType.COMMIT and not commit.lower().startswith(ref.lower()):
                raise ValueError("Locked commit does not match the requested prefix")
            return commit.lower()
        if self._resolver is None:
            raise ValueError("No reference resolver is available")
        if ref_type == GitReferenceType.COMMIT:
            commit = self._resolver.resolve_git_reference(dep).resolved_commit
            if not is_full_revision_pin(commit) or not commit.lower().startswith(ref.lower()):
                raise ValueError("Resolved commit does not match the requested prefix")
        else:
            repo_key = replace(dep, virtual_path=None, alias=None).get_unique_key()
            if repo_key not in self._remote_refs:
                index: dict[tuple[str, GitReferenceType], list[RemoteRef]] = {}
                for remote in self._resolver.list_remote_refs(dep):
                    index.setdefault((remote.name, remote.ref_type), []).append(remote)
                self._remote_refs[repo_key] = index
            refs = self._remote_refs[repo_key]
            explicit_tag = ref.startswith("refs/tags/")
            explicit_branch = ref.startswith("refs/heads/")
            name = ref.removeprefix("refs/tags/").removeprefix("refs/heads/")
            if not explicit_tag and (name, GitReferenceType.BRANCH) in refs:
                return None
            tags = refs.get((name, GitReferenceType.TAG), [])
            if explicit_branch or len(tags) != 1:
                raise ValueError("Requested ref is absent or ambiguous")
            commit = tags[0].commit_sha
        if not is_full_revision_pin(commit):
            raise ValueError("Reference did not resolve to a full commit")
        return commit.lower()
