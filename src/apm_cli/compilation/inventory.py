"""Read-only project inventory shared by compilation phases."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..utils.exclude import should_exclude, validate_exclude_patterns
from ..utils.paths import relative_to

_UNIVERSALLY_SKIPPED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
    }
)


@dataclass(frozen=True)
class InventoryDirectory:
    """One directory observed during a compilation inventory walk."""

    path: Path
    relative_path: Path
    depth: int
    child_names: tuple[str, ...]
    file_names: tuple[str, ...]


def _seeded_listing(root: Path):
    from ..utils.content_hash import seeded_listing

    return seeded_listing(root)


def _seeded_walk(root: Path, listing: dict[str, list[str]]):
    """PERF B3 PROTOTYPE: ``os.walk`` (top-down, honours pruning) over a proven seeded tree."""
    children: dict[str, list[str]] = {}
    for rel in listing:
        if rel:
            parent, _, name = rel.rpartition("/")
            children.setdefault(parent, []).append(name)
    stack = [""]
    while stack:
        rel = stack.pop()
        dirnames = list(children.get(rel, ()))
        filenames = list(listing.get(rel, ()))
        yield (os.path.join(str(root), *rel.split("/")) if rel else str(root)), dirnames, filenames
        stack.extend(f"{rel}/{name}" if rel else name for name in reversed(dirnames))


def _substituting_walk(root: Path):
    """PERF B3 PROTOTYPE: ``os.walk`` that swaps in the seeded listing below any proven
    seeded package root it reaches (pre-order and pruning preserved)."""
    from ..utils.content_hash import is_seed_root, seeded_listing

    walker = os.walk(root, followlinks=False)
    for directory, child_dirs, file_names in walker:
        listing = seeded_listing(directory) if is_seed_root(directory) else None
        if listing is None:
            yield directory, child_dirs, file_names
            continue
        child_dirs[:] = []  # os.walk does not descend; the seeded subtree replaces it
        yield from _seeded_walk(Path(directory), listing)


# PERF B3 PROTOTYPE: compute the walk too and fail loudly on any difference.
_B3_DIFFERENTIAL = False


@dataclass(frozen=True)
class CompileInventory:
    """Deterministic, read-only filesystem snapshot for one compile invocation.

    The inventory owns the active-checkout boundary. Nested Git repositories
    remain recorded as roots but their contents are never admitted to consumers.
    """

    root: Path
    directories: tuple[InventoryDirectory, ...]
    nested_repository_roots: frozenset[Path]
    _directory_positions: dict[Path, int]

    @classmethod
    def collect(
        cls,
        root: Path,
        *,
        exclude_patterns: list[str] | None = None,
        _force_walk: bool = False,
    ) -> CompileInventory:
        """Collect one exclusion-aware, symlink-safe project snapshot."""
        try:
            root = root.resolve()
        except OSError:
            root = root.absolute()
        safe_patterns = validate_exclude_patterns(exclude_patterns)
        directories: list[InventoryDirectory] = []
        nested_repository_roots: set[Path] = set()

        listing = None if _force_walk else _seeded_listing(root)
        walk = (
            _seeded_walk(root, listing)
            if listing is not None
            else (os.walk(root, followlinks=False) if _force_walk else _substituting_walk(root))
        )
        for directory, child_dirs, file_names in walk:
            path = Path(directory)
            if should_exclude(path, root, safe_patterns):
                child_dirs[:] = []
                continue
            if path != root and (".git" in file_names or ".git" in child_dirs):
                nested_repository_roots.add(path)
                child_dirs[:] = []
                continue

            admitted_children = sorted(
                name
                for name in child_dirs
                if name not in _UNIVERSALLY_SKIPPED_DIRS
                and not should_exclude(path / name, root, safe_patterns)
            )
            child_dirs[:] = admitted_children
            relative_path = relative_to(path, root)
            directories.append(
                InventoryDirectory(
                    path=path,
                    relative_path=relative_path,
                    depth=len(relative_path.parts),
                    child_names=tuple(admitted_children),
                    file_names=tuple(sorted(file_names)),
                )
            )

        inventory = cls(
            root=root,
            directories=tuple(directories),
            nested_repository_roots=frozenset(nested_repository_roots),
            _directory_positions={entry.path: index for index, entry in enumerate(directories)},
        )
        if not _force_walk and _B3_DIFFERENTIAL:
            walked = cls.collect(root, exclude_patterns=exclude_patterns, _force_walk=True)
            if (
                walked.directories != inventory.directories
                or walked.nested_repository_roots != inventory.nested_repository_roots
            ):
                raise RuntimeError(
                    f"PERF B3 differential mismatch: seeded listing != walk at {root}"
                )
        return inventory

    def contains_directory(self, path: Path) -> bool:
        """Return whether *path* was observed as a directory."""
        return path in self._directory_positions

    def nested_repository_root_for(self, path: Path) -> Path | None:
        """Return the nested repository containing *path*, if any."""
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        try:
            relative_to(resolved, self.root)
        except ValueError:
            return None
        current = resolved
        while current != self.root:
            if current in self.nested_repository_roots:
                return current
            current = current.parent
        return None

    def files_under(self, roots: frozenset[str] | None = None) -> tuple[Path, ...]:
        """Return non-hidden candidate files, optionally under literal roots."""
        files: list[Path] = []
        for entry in self.directories:
            if roots is not None and (
                not entry.relative_path.parts or entry.relative_path.parts[0] not in roots
            ):
                continue
            files.extend(entry.path / name for name in entry.file_names if not name.startswith("."))
        return tuple(files)

    def files_within(self, directory: Path) -> tuple[Path, ...]:
        """Return every recorded file beneath an inventory directory."""
        try:
            resolved_directory = directory.resolve()
            relative_directory = relative_to(resolved_directory, self.root)
        except (OSError, ValueError):
            return ()
        start = self._directory_positions.get(resolved_directory)
        if start is None:
            return ()
        files: list[Path] = []
        # os.walk emits each directory's subtree contiguously in this snapshot.
        for entry in self.directories[start:]:
            try:
                entry.relative_path.relative_to(relative_directory)
            except ValueError:
                break
            files.extend(entry.path / name for name in entry.file_names)
        return tuple(files)
