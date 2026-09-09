"""Read-only snapshots and assertions for filesystem artifact tests.

For lifecycle-aware manifest, lock, config, and deployment state, use
``LifecycleStateSnapshot`` instead.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Literal, TypeAlias

ArtifactKind: TypeAlias = Literal["file", "directory", "symlink"]


@dataclass(frozen=True)
class ArtifactEntry:
    """Immutable metadata for one path below a snapshot root."""

    relative_path: str
    kind: ArtifactKind
    fingerprint: str | None


@dataclass(frozen=True)
class ArtifactDiff:
    """Immutable sets of entry-level changes between two snapshots."""

    added: frozenset[str]
    removed: frozenset[str]
    changed: frozenset[str]


@dataclass(frozen=True)
class ArtifactSnapshot:
    """Immutable capture of the real filesystem below one root."""

    root: Path
    root_existed: bool
    entries: tuple[ArtifactEntry, ...]

    @classmethod
    def capture(cls, root: Path) -> ArtifactSnapshot:
        """Capture path kinds and exact content hashes without writing to disk."""
        if not root.exists():
            return cls(root=root, root_existed=False, entries=())

        entries = []
        paths = sorted(root.rglob("*"), key=lambda path: _portable_path(path.relative_to(root)))
        for path in paths:
            relative_path = _portable_path(path.relative_to(root))
            if path.is_symlink():
                entry = ArtifactEntry(
                    relative_path=relative_path,
                    kind="symlink",
                    fingerprint=os.readlink(path),
                )
            elif path.is_dir():
                entry = ArtifactEntry(
                    relative_path=relative_path,
                    kind="directory",
                    fingerprint=None,
                )
            else:
                entry = ArtifactEntry(
                    relative_path=relative_path,
                    kind="file",
                    fingerprint=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            entries.append(entry)
        return cls(root=root, root_existed=True, entries=tuple(entries))

    @property
    def paths(self) -> frozenset[str]:
        """Return all captured paths in portable POSIX form."""
        return frozenset(entry.relative_path for entry in self.entries)

    def diff(self, after: ArtifactSnapshot) -> ArtifactDiff:
        """Compare this capture with a later entry-level capture."""
        _require_snapshot(self)
        _require_snapshot(after)
        before_entries = {entry.relative_path: entry for entry in self.entries}
        after_entries = {entry.relative_path: entry for entry in after.entries}
        common_paths = before_entries.keys() & after_entries.keys()
        return ArtifactDiff(
            added=frozenset(after_entries.keys() - before_entries.keys()),
            removed=frozenset(before_entries.keys() - after_entries.keys()),
            changed=frozenset(
                path for path in common_paths if before_entries[path] != after_entries[path]
            ),
        )


@dataclass(frozen=True)
class ArtifactSnapshotSet:
    """Open-world filesystem captures for explicitly named, non-overlapping roots."""

    snapshots: tuple[tuple[str, ArtifactSnapshot], ...]

    @classmethod
    def capture(cls, roots: Mapping[str, Path]) -> ArtifactSnapshotSet:
        """Capture every entry below each named root without consulting product state."""
        if not roots:
            raise ValueError("artifact snapshot roots must not be empty")
        normalized: list[tuple[str, Path]] = []
        resolved_paths: list[tuple[str, Path]] = []
        for root_id, root in sorted(roots.items()):
            if not root_id:
                raise ValueError("artifact snapshot root IDs must not be empty")
            absolute = root.absolute()
            resolved = absolute.resolve()
            for existing_id, existing in resolved_paths:
                if _paths_overlap(resolved, existing):
                    raise ValueError(
                        f"artifact snapshot roots must not overlap: {root_id!r} and {existing_id!r}"
                    )
            normalized.append((root_id, absolute))
            resolved_paths.append((root_id, resolved))
        return cls(
            snapshots=tuple(
                (root_id, ArtifactSnapshot.capture(root)) for root_id, root in normalized
            )
        )

    def snapshot(self, root_id: str) -> ArtifactSnapshot:
        """Return one named capture, failing clearly for unknown roots."""
        for candidate, snapshot in self.snapshots:
            if candidate == root_id:
                return snapshot
        known = ", ".join(candidate for candidate, _snapshot in self.snapshots)
        raise KeyError(f"Unknown artifact snapshot root {root_id!r}; known roots: {known}")


def assert_paths_present(
    snapshot: ArtifactSnapshot,
    expected_paths: Collection[str],
) -> None:
    """Assert that every expected portable path exists in a capture."""
    _require_snapshot(snapshot)
    missing = set(expected_paths) - snapshot.paths
    assert not missing, f"Missing artifact paths: {sorted(missing)}"


def assert_paths_absent(
    snapshot: ArtifactSnapshot,
    unexpected_paths: Collection[str],
) -> None:
    """Assert that no unexpected portable path exists in a capture."""
    _require_snapshot(snapshot)
    present = set(unexpected_paths) & snapshot.paths
    assert not present, f"Unexpected artifact paths: {sorted(present)}"


def assert_paths_created(
    before: ArtifactSnapshot,
    after: ArtifactSnapshot,
    expected_paths: Collection[str],
) -> None:
    """Assert that every expected portable path was newly created."""
    _require_snapshot(before)
    _require_snapshot(after)
    created = before.diff(after).added
    missing = set(expected_paths) - created
    assert not missing, f"Paths were not created: {sorted(missing)}"


def assert_unchanged(
    before: ArtifactSnapshot,
    after: ArtifactSnapshot,
) -> None:
    """Assert root identity, root existence, and all entries are unchanged."""
    _require_snapshot(before)
    _require_snapshot(after)
    assert before.root == after.root, (
        f"assert_unchanged requires the same root: {before.root} != {after.root}"
    )
    assert before.root_existed == after.root_existed, (
        f"Root existence changed for {before.root}: {before.root_existed} -> {after.root_existed}"
    )
    difference = before.diff(after)
    assert difference == ArtifactDiff(frozenset(), frozenset(), frozenset()), (
        f"Unexpected artifact changes for {before.root}: {difference}"
    )


def assert_only_paths_changed(
    before: ArtifactSnapshot,
    after: ArtifactSnapshot,
    allowed_paths: Collection[str],
) -> None:
    """Assert that all entry-level changes are confined to allowed paths."""
    _require_snapshot(before)
    _require_snapshot(after)
    difference = before.diff(after)
    observed = difference.added | difference.removed | difference.changed
    unexpected = observed - set(allowed_paths)
    assert not unexpected, f"Unexpected changed paths: {sorted(unexpected)}"


def assert_snapshot_set_unchanged(
    before: ArtifactSnapshotSet,
    after: ArtifactSnapshotSet,
) -> None:
    """Assert every named open-world filesystem root is byte-identical."""
    _require_snapshot_set_pair(before, after)
    for root_id, snapshot in before.snapshots:
        assert_unchanged(snapshot, after.snapshot(root_id))


def assert_only_snapshot_paths_changed(
    before: ArtifactSnapshotSet,
    after: ArtifactSnapshotSet,
    allowed_paths: Mapping[str, Collection[str]],
) -> None:
    """Assert all changes across named roots are confined to explicit paths."""
    _require_snapshot_set_pair(before, after)
    unknown = set(allowed_paths) - {root_id for root_id, _snapshot in before.snapshots}
    if unknown:
        raise KeyError(f"Unknown artifact snapshot roots in allowlist: {sorted(unknown)}")
    for root_id, snapshot in before.snapshots:
        assert_only_paths_changed(
            snapshot,
            after.snapshot(root_id),
            allowed_paths.get(root_id, ()),
        )


def assert_snapshot_changes_within(
    before: ArtifactSnapshotSet,
    after: ArtifactSnapshotSet,
    *,
    exact_paths: Mapping[str, Collection[str]],
    tree_prefixes: Mapping[str, Collection[str]],
) -> None:
    """Assert changes stay within exact paths or explicitly allowed subtrees."""
    _require_snapshot_set_pair(before, after)
    known_roots = {root_id for root_id, _snapshot in before.snapshots}
    unknown = (set(exact_paths) | set(tree_prefixes)) - known_roots
    if unknown:
        raise KeyError(f"Unknown artifact snapshot roots in write set: {sorted(unknown)}")
    for root_id, snapshot in before.snapshots:
        difference = snapshot.diff(after.snapshot(root_id))
        observed = difference.added | difference.removed | difference.changed
        allowed_exact = set(exact_paths.get(root_id, ()))
        allowed_trees = set(tree_prefixes.get(root_id, ()))
        unexpected = {
            path
            for path in observed
            if path not in allowed_exact
            and not any(_is_path_in_tree(path, prefix) for prefix in allowed_trees)
        }
        assert not unexpected, (
            f"Artifact root {root_id!r} wrote outside its declared write set: {sorted(unexpected)}"
        )


def _require_snapshot(value: object) -> None:
    """Reject authored mappings in place of real filesystem captures."""
    if not isinstance(value, ArtifactSnapshot):
        raise TypeError("artifact assertions require ArtifactSnapshot instances")


def _require_snapshot_set_pair(
    before: object,
    after: object,
) -> None:
    if not isinstance(before, ArtifactSnapshotSet) or not isinstance(after, ArtifactSnapshotSet):
        raise TypeError("artifact snapshot set assertions require ArtifactSnapshotSet instances")
    before_ids = tuple(root_id for root_id, _snapshot in before.snapshots)
    after_ids = tuple(root_id for root_id, _snapshot in after.snapshots)
    if before_ids != after_ids:
        raise ValueError(f"artifact snapshot root IDs differ: {before_ids!r} != {after_ids!r}")


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _is_path_in_tree(path: str, prefix: str) -> bool:
    return bool(prefix) and (path == prefix or path.startswith(f"{prefix}/"))


def _portable_path(path: PurePath) -> str:
    """Render any pathlib path flavor with portable separators."""
    return path.as_posix()
