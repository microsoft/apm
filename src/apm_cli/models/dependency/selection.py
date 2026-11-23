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
    ``manifest_declarations_of`` returns every declaration to remove. Callers
    selecting many identifiers build one :class:`ManifestSelectionIndex`.
    """
    return ManifestSelectionIndex(manifest_dependencies, lockfile).select(package)


class ManifestSelectionIndex:
    """One parse of the manifest and lockfile, answering many uninstall selections.

    :meth:`select` gives the same answer as :func:`select_manifest_dependency`
    in time proportional to its matches, not to the manifest and lock sizes.
    """

    def __init__(self, manifest_dependencies: list[object], lockfile: object | None) -> None:
        self._by_identity: dict[str, list[tuple[int, object]]] = {}
        self._unparsed: dict[str, list[tuple[int, object]]] = {}
        self._local_by_key: dict[str, list[tuple[int, object]]] = {}
        for index, entry in enumerate(manifest_dependencies):
            try:
                dependency = parse_dependency_entry(entry)
            except (ValueError, TypeError, AttributeError, KeyError):
                text = entry if isinstance(entry, str) else str(entry)
                self._unparsed.setdefault(text, []).append((index, entry))
                continue
            self._by_identity.setdefault(dependency.get_identity(), []).append((index, entry))
            if dependency.is_local:
                key = dependency.get_unique_key()
                self._local_by_key.setdefault(key, []).append((index, entry))
        self._locked_local: dict[str, set[str]] = {}
        locked_dependencies = getattr(lockfile, "dependencies", None)
        if isinstance(locked_dependencies, dict):
            for dependency in locked_dependencies.values():
                if getattr(dependency, "source", None) != "local":
                    continue
                try:
                    key = dependency.get_unique_key()
                except (ValueError, TypeError, AttributeError, KeyError):
                    continue
                repo_url = getattr(dependency, "repo_url", None)
                self._locked_local.setdefault(repo_url, set()).add(key)

    def select(self, package: str) -> DependencySelection:
        """Select exactly one manifest entry for *package* (see the module function)."""
        try:
            requested_identity = DependencyReference.parse(package).get_identity()
        except (ValueError, TypeError, AttributeError, KeyError):
            requested_identity = package
        direct = sorted(
            [*self._by_identity.get(requested_identity, ()), *self._unparsed.get(package, ())],
            key=lambda item: item[0],
        )
        matched: dict[int, object] = dict(direct)
        aliased = sorted(
            (
                item
                for key in self._locked_local.get(package, ())
                for item in self._local_by_key.get(key, ())
            ),
            key=lambda item: item[0],
        )
        for index, entry in aliased:
            matched.setdefault(index, entry)
        return _selection_from_matches(list(matched.values()))


def select_marketplace_declaration(
    plugin_name: str,
    marketplace_name: str,
    manifest_dependencies: list[object],
) -> DependencySelection:
    """Select the object-form ``{name, marketplace}`` declaration for ``plugin@marketplace``.

    Matching is case-insensitive, like registry and catalog lookups, and reads
    only the manifest: no lockfile, registry or catalog is consulted.
    """
    alias, plugin = marketplace_name.casefold(), plugin_name.casefold()
    matches: list[object] = []
    for entry in manifest_dependencies:
        try:
            declared = parse_dependency_entry(entry)
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if (
            declared.is_marketplace
            and (declared.marketplace_name or "").casefold() == alias
            and (declared.marketplace_plugin_name or "").casefold() == plugin
        ):
            matches.append(entry)
    return _selection_from_matches(matches)


def _selection_from_matches(matches: list[object]) -> DependencySelection:
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
