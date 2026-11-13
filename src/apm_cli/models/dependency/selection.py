"""Canonical manifest dependency selection for CLI operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .reference import DependencyReference


class DependencySelectionStatus(str, Enum):
    """Outcome of matching one CLI identifier to manifest dependencies."""

    MATCHED = "matched"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class DependencySelection:
    """One deterministic manifest dependency selection result."""

    status: DependencySelectionStatus
    manifest_entry: object | None
    match_count: int


def parse_dependency_entry(dep_entry: object) -> DependencyReference:
    """Parse one supported manifest dependency entry through the model owner."""
    if isinstance(dep_entry, DependencyReference):
        return dep_entry
    if isinstance(dep_entry, str):
        return DependencyReference.parse(dep_entry)
    if isinstance(dep_entry, dict):
        return DependencyReference.parse_from_dict(dep_entry)
    raise ValueError(f"Unsupported dependency entry type: {type(dep_entry).__name__}")


def select_manifest_dependency(
    package: str,
    manifest_dependencies: list[object],
    lockfile: object | None,
) -> DependencySelection:
    """Select exactly one manifest entry for an uninstall identifier.

    Exact declared identifiers use ``DependencyReference`` identity semantics.
    A portable local alias such as ``_local/pkg`` is accepted only when persisted
    lock metadata maps that alias back to one declared local dependency key.
    Ambiguous aliases fail closed instead of guessing. One package declared
    more than once (in both sections, or under two spellings) is one match;
    ``manifest_declarations_of`` returns every declaration to remove.
    """
    try:
        requested_identity = DependencyReference.parse(package).get_identity()
    except (ValueError, TypeError, AttributeError, KeyError):
        requested_identity = package

    parsed_entries: list[tuple[int, object, DependencyReference]] = []
    matched_entries: dict[int, object] = {}
    for index, entry in enumerate(manifest_dependencies):
        try:
            dependency = parse_dependency_entry(entry)
        except (ValueError, TypeError, AttributeError, KeyError):
            if (entry if isinstance(entry, str) else str(entry)) == package:
                matched_entries[index] = entry
            continue
        parsed_entries.append((index, entry, dependency))
        if dependency.get_identity() == requested_identity:
            matched_entries[index] = entry

    locked_alias_keys: set[str] = set()
    locked_dependencies = getattr(lockfile, "dependencies", None)
    if isinstance(locked_dependencies, dict):
        for dependency in locked_dependencies.values():
            if (
                getattr(dependency, "source", None) != "local"
                or getattr(dependency, "repo_url", None) != package
            ):
                continue
            try:
                locked_alias_keys.add(dependency.get_unique_key())
            except (ValueError, TypeError, AttributeError, KeyError):
                continue

    if locked_alias_keys:
        for index, entry, dependency in parsed_entries:
            if dependency.is_local and dependency.get_unique_key() in locked_alias_keys:
                matched_entries[index] = entry

    matches = list(matched_entries.values())
    if matches and len(manifest_declarations_of(matches, matches[:1])) == len(matches):
        return DependencySelection(
            status=DependencySelectionStatus.MATCHED,
            manifest_entry=matches[0],
            match_count=len(matches),
        )
    if len(matches) > 1:
        return DependencySelection(
            status=DependencySelectionStatus.AMBIGUOUS,
            manifest_entry=None,
            match_count=len(matches),
        )
    return DependencySelection(
        status=DependencySelectionStatus.NOT_FOUND,
        manifest_entry=None,
        match_count=0,
    )


def _declared_identity(dep_entry: object) -> str | None:
    try:
        return parse_dependency_entry(dep_entry).get_identity()
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def manifest_declarations_of(
    manifest_dependencies: list[object],
    selected: list[object],
) -> list[object]:
    """Return every manifest declaration of the packages named by *selected*.

    Declarations match by ``DependencyReference`` identity, so a package listed
    in both ``dependencies`` and ``devDependencies`` or under two spellings is
    removed as a whole. Unparseable entries match only themselves.
    """
    identities = {_declared_identity(entry) for entry in selected} - {None}
    return [
        entry
        for entry in manifest_dependencies
        if (identity := _declared_identity(entry)) in identities
        or (identity is None and entry in selected)
    ]
